"""WSFEv1 (Web Service de Factura Electrónica) client.

Handles:
- FECompUltimoAutorizado: get last authorized comprobante number
- FECAESolicitar: authorize new comprobante → get CAE
- FECompConsultar: query existing comprobante (for recovery)
- FEParamGetCotizacion: get official AFIP/BNA exchange rate
"""
import datetime
import ssl
import sys
from typing import Optional

import requests
import zeep
from zeep.transports import Transport


# WSFEv1 endpoints
WSFE_URLS = {
    "homologacion": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx?WSDL",
    "produccion": "https://servicios1.afip.gov.ar/wsfev1/service.asmx?WSDL",
}


def _afip_session() -> requests.Session:
    """Create a requests Session that tolerates AFIP's weak DH keys."""
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(
        max_retries=2,
    ))
    # Patch the session's underlying SSL context
    session.verify = True
    # Use a custom adapter that injects our SSL context
    from urllib3.util.ssl_ import create_urllib3_context
    _orig_init_ctx = requests.adapters.HTTPAdapter.init_poolmanager

    class _AFIPAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = ctx
            return super().init_poolmanager(*args, **kwargs)

    session.mount("https://", _AFIPAdapter(max_retries=2))
    return session


class WSFEClient:
    """WSFEv1 SOAP client."""

    def __init__(self, token: str, sign: str, cuit: str, ambiente: str):
        self.token = token
        self.sign = sign
        self.cuit = int(cuit)
        self.ambiente = ambiente

        wsdl_url = WSFE_URLS[ambiente]
        session = _afip_session()
        transport = Transport(session=session)
        self.client = zeep.Client(wsdl=wsdl_url, transport=transport)
        self.service = self.client.service

    def _auth(self) -> dict:
        """Build the Auth structure for WSFE calls."""
        return {
            "Token": self.token,
            "Sign": self.sign,
            "Cuit": self.cuit,
        }

    def get_ultimo_autorizado(self, punto_venta: int, tipo_cbte: int) -> int:
        """Get the last authorized comprobante number.

        Returns the number (0 if none emitted yet for this type+PV).
        """
        response = self.service.FECompUltimoAutorizado(
            Auth=self._auth(),
            PtoVta=punto_venta,
            CbteTipo=tipo_cbte,
        )

        self._check_errors(response)
        return response.CbteNro

    def solicitar_cae(
        self,
        punto_venta: int,
        tipo_cbte: int,
        concepto: int,  # 1=Productos, 2=Servicios, 3=Ambos
        doc_tipo: int,  # 80=CUIT, 99=Sin identificar
        doc_nro: int,
        cbte_desde: int,
        cbte_hasta: int,
        cbte_fch: str,  # YYYYMMDD
        imp_total: float,
        imp_neto: float,
        imp_iva: float = 0,
        imp_trib: float = 0,
        imp_op_ex: float = 0,
        mon_id: str = "PES",  # PES or DOL
        mon_cotiz: float = 1,
        fch_serv_desde: str = "",  # YYYYMMDD (required for Concepto 2,3)
        fch_serv_hasta: str = "",
        fch_vto_pago: str = "",
        opcionales: Optional[list] = None,
        cbtes_asoc: Optional[list] = None,
    ) -> dict:
        """Authorize a new comprobante and get CAE.

        Returns dict with cae, cae_fch_vto, cbte_nro, resultado.
        """
        # Build FECAEDetRequest
        det = {
            "Concepto": concepto,
            "DocTipo": doc_tipo,
            "DocNro": doc_nro,
            "CbteDesde": cbte_desde,
            "CbteHasta": cbte_hasta,
            "CbteFch": cbte_fch,
            "ImpTotal": imp_total,
            "ImpTotConc": 0,
            "ImpNeto": imp_neto,
            "ImpOpEx": imp_op_ex,
            "ImpTrib": imp_trib,
            "ImpIVA": imp_iva,
            "MonId": mon_id,
            "MonCotiz": mon_cotiz,
        }

        # Add service dates if Concepto is services (2 or 3)
        if concepto in (2, 3):
            det["FchServDesde"] = fch_serv_desde
            det["FchServHasta"] = fch_serv_hasta
            # FchVtoPago must NOT be sent for Nota de Crédito (tipos 13, 213)
            if tipo_cbte not in (13, 213):
                det["FchVtoPago"] = fch_vto_pago

        # Add optional fields (for FCE MiPyMEs)
        if opcionales:
            det["Opcionales"] = {"Opcional": opcionales}

        # Add associated comprobantes (for NC)
        if cbtes_asoc:
            det["CbtesAsoc"] = {"CbteAsoc": cbtes_asoc}

        # Build request
        request = {
            "FeCabReq": {
                "CantReg": 1,
                "PtoVta": punto_venta,
                "CbteTipo": tipo_cbte,
            },
            "FeDetReq": {
                "FECAEDetRequest": [det],
            },
        }

        response = self.service.FECAESolicitar(
            Auth=self._auth(),
            FeCAEReq=request,
        )

        self._check_errors(response)

        # Extract result from first (only) detail
        det_resp = response.FeDetResp.FECAEDetResponse[0]

        if det_resp.Resultado != "A":  # A=Aprobado, R=Rechazado
            obs = ""
            if det_resp.Observaciones:
                obs_list = det_resp.Observaciones.Obs
                obs = "; ".join(f"[{o.Code}] {o.Msg}" for o in obs_list)
            sys.exit(f"Error: AFIP rejected comprobante. Resultado: {det_resp.Resultado}\nObservaciones: {obs}")

        return {
            "cae": det_resp.CAE,
            "cae_fch_vto": det_resp.CAEFchVto,
            "cbte_nro": det_resp.CbteDesde,
            "resultado": det_resp.Resultado,
        }

    def consultar_comprobante(
        self, punto_venta: int, tipo_cbte: int, cbte_nro: int
    ) -> Optional[dict]:
        """Query an existing comprobante (for recovery).

        Returns dict with comprobante data, or None if not found.
        """
        try:
            response = self.service.FECompConsultar(
                Auth=self._auth(),
                FeCompConsReq={
                    "CbteTipo": tipo_cbte,
                    "CbteNro": cbte_nro,
                    "PtoVta": punto_venta,
                },
            )

            self._check_errors(response)

            result = response.ResultGet
            if result:
                return {
                    "cae": result.CodAutorizacion,
                    "cae_fch_vto": result.FchVto,
                    "cbte_nro": result.CbteDesde,
                    "imp_total": result.ImpTotal,
                    "cbte_fch": result.CbteFch,
                    "resultado": result.Resultado,
                }
        except Exception:
            pass

        return None

    def get_cotizacion(self, mon_id: str = "DOL") -> float:
        """Get official AFIP/BNA exchange rate.

        Returns the cotización vendedor.
        """
        response = self.service.FEParamGetCotizacion(
            Auth=self._auth(),
            MonId=mon_id,
        )

        self._check_errors(response)

        if response.ResultGet:
            return float(response.ResultGet.MonCotiz)

        raise RuntimeError(f"AFIP returned no cotizacion for {mon_id}")

    def _check_errors(self, response) -> None:
        """Check WSFE response for errors."""
        if hasattr(response, "Errors") and response.Errors:
            errors = response.Errors.Err
            msgs = "; ".join(f"[{e.Code}] {e.Msg}" for e in errors)
            sys.exit(f"AFIP Error: {msgs}")
