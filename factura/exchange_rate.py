"""Exchange rate fetcher — BNA official rate (NOT MEP/blue/CCL).

RG 4291/2018 requires BNA vendedor rate for AFIP invoicing.
For live mode, use FEParamGetCotizacion from WSFEv1 instead.
"""
import requests


def get_bna_rate_dolarapi() -> float:
    """Fetch official BNA vendedor rate from dolarapi.com.

    This is the dry-run fallback. For live invoicing, use FEParamGetCotizacion.
    """
    try:
        resp = requests.get(
            "https://dolarapi.com/v1/dolares/oficial",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["venta"])
        if rate <= 0:
            raise ValueError(f"Invalid rate: {rate}")
        return rate
    except Exception as e:
        raise RuntimeError(f"Failed to fetch BNA rate from dolarapi: {e}") from e


def get_bna_rate_bluelytics() -> float:
    """Fallback: fetch BNA rate from bluelytics API."""
    try:
        resp = requests.get(
            "https://api.bluelytics.com.ar/v2/latest",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["oficial"]["value_sell"])
        if rate <= 0:
            raise ValueError(f"Invalid rate: {rate}")
        return rate
    except Exception as e:
        raise RuntimeError(f"Failed to fetch BNA rate from bluelytics: {e}") from e


def get_exchange_rate() -> float:
    """Get the official BNA vendedor exchange rate (USD→ARS).

    Tries dolarapi first, then bluelytics as fallback.
    For AFIP live mode, use afip_wsfe.get_cotizacion() instead.
    """
    try:
        return get_bna_rate_dolarapi()
    except RuntimeError:
        return get_bna_rate_bluelytics()
