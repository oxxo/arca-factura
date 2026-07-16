# arca-factura

CLI en Python para emitir comprobantes electrónicos contra los web services de AFIP (hoy ARCA): Factura C, Factura de Crédito Electrónica MiPyMEs (FCE) y sus notas de crédito, con CAE real vía WSAA + WSFEv1, PDF con QR oficial, y un ledger local de idempotencia que hace inofensivo repetir el mismo comando.

```bash
python -m factura acme 3
# → autentica contra WSAA, pide CAE por WSFEv1, genera el PDF y registra la emisión
```

Pensada para monotributistas / freelancers que facturan pagos por proyecto y quieren automatizar la emisión sin renunciar al control: cada corrida deja rastro, repetir el mismo comando es inofensivo, y siempre se puede ensayar sin tocar AFIP.

*(English version: [README.md](README.md))*

> **Nota sobre nombres:** AFIP fue reemplazada por ARCA, pero los web services, endpoints y documentación técnica siguen usando la marca AFIP. Este README usa "AFIP" para referirse a los servicios (WSAA, WSFEv1) y "ARCA" para el portal donde se gestionan certificados.

## Por qué existe

Facturar a mano desde el portal web es lento, propenso a error (¿qué cotización uso?, ¿ya emití este pago?) y no deja registro estructurado. Las alternativas típicas — SDKs abandonados, servicios pagos, o el portal — no resuelven el problema real: **la gobernanza de la emisión**. Un comprobante fiscal es irreversible; el costo de un error no está en la dificultad técnica sino en emitir de más, emitir dos veces, o emitir con la cotización equivocada.

Esta herramienta ataca eso directamente:

- Los datos de facturación de cada cliente viven en un registro externo (YAML) que la herramienta **lee y nunca escribe**.
- Cada emisión queda registrada en un ledger de idempotencia: repetir el comando es inofensivo.
- Todo movimiento — incluso los ensayos — queda en un audit log append-only.
- El modo `--dry-run` genera el PDF completo sin conectarse a AFIP, para revisar antes de emitir.

## Features

### Gobernanza

- **Ledger de idempotencia** (`invoices.yaml`): cada comprobante se registra bajo la clave `{proyecto}-{pago}` (las NC agregan sufijo `-nc`). La clave usa el slug **tal como se tipeó** — el matcher acepta varias escrituras (`acme` y `acme-001` matchean el mismo proyecto) pero generan claves distintas, así que usá siempre la misma escritura: la idempotencia es por comando exacto, no por proyecto. Si el pago ya fue facturado, la corrida imprime `*** YA EMITIDO ***` con comprobante, CAE, fecha y archivo, y termina con exit 0. Re-ejecutar es siempre seguro.
- **Audit log append-only** (`factura/audit.log`): una línea por corrida **completada**, con timestamp ISO y evento — `DRY_RUN`, `DRY_RUN_NC`, `EMITIDA` (con cbte, CAE, tipo, monto, ambiente y PDF) o `NC_EMITIDA`. Las corridas que terminan antes (`YA EMITIDO`, errores de config, rechazos de AFIP) no dejan línea. Los dry-runs quedan en el log pero **no** en el ledger; solo las emisiones reales tocan `invoices.yaml`.
- **Dry-run** (`--dry-run`): cero conexión a AFIP. Genera el PDF con número placeholder `XXXXXXXX` y la leyenda en rojo *"BORRADOR (SIN CAE) — Este documento no tiene validez fiscal"*.
- **Homologación** (`--homo`): corre el flujo completo (WSAA + WSFEv1) contra los endpoints de prueba de AFIP.
- **Registro de clientes read-only**: los datos de facturación (razón social, CUIT, condición IVA, tipo de comprobante, plan de pagos) se leen de un `projects-registry.yaml` externo que la herramienta jamás modifica.
- **Guardrail de Monotributo**: al final de cada emisión o dry-run de factura (no en las corridas de NC ni en las que terminan en `YA EMITIDO`) suma la facturación ARS del año desde el ledger (excluyendo NC) y avisa si supera el 80 % del tope de la categoría H. *Los topes están hardcodeados en `cli.py` (valores aproximados 2025/2026) — actualizalos a mano cuando ARCA los recategorice.*

### Emisión

- **Tipos de comprobante**: 11 (Factura C), 211 (FCE MiPyMEs C), y sus notas de crédito 13 y 213.
- **Notas de crédito** (`--nc`): exige que la factura original exista en el ledger, mapea el tipo automáticamente (11→13, 211→213), arma el `CbtesAsoc` referenciando el comprobante original, y registra bajo su propia clave de idempotencia. Combinable con `--dry-run`.
- **FCE MiPyMEs**: las facturas 211 envían los Opcionales requeridos (2101 = CBU, 2102 = alias, 27 = opción de transferencia, default `SCA`) y el PDF incluye el bloque de datos de pago. Las NC (13 y 213 por igual) envían únicamente el Opcional 22 = `S` (anulación).
- **Facturación en USD**: comprobantes con `MonId=DOL` y la cotización como `MonCotiz`. Estrategia en dos capas conforme RG 4291/2018 (cotización BNA vendedor, no MEP/blue):
  1. Estimación pre-emisión desde dolarapi.com (oficial venta), con fallback a bluelytics y, en último caso, prompt interactivo.
  2. En modo live, la cotización oficial de AFIP (`FEParamGetCotizacion`) **pisa** la estimación. Si el servicio falla, se mantiene la estimación con un warning.
  - `--cotizacion` saltea ambas capas (p. ej., MEP pactado por contrato); `--monto` fuerza pesos (`MonId=PES`, cotización 1.0).
- **PDF con QR oficial**: plantilla Jinja2 + WeasyPrint (A4, márgenes 15 mm), QR de verificación de AFIP (payload JSON v1 en base64 apuntando a `https://www.afip.gob.ar/fe/qr/`), nomenclatura estándar `{CUIT}_{tipo:03d}_{PV:05d}_{nro:08d}.pdf`, guardado en `{proyecto}/docs/facturas/` (o en `pdf.output_base`).

### Infraestructura

- **WSAA sin herramientas externas**: el TRA (XML, validez 12 h, timestamps ART) se firma en CMS/PKCS#7 con la librería `cryptography` (SHA256, DER) y se envía por SOAP (`zeep`). El token se cachea en `~/.afip/token_cache.json` con margen de expiración de 5 minutos, así las corridas dentro de la ventana de 12 h no repiten el login.
- **Pre-flight del certificado**: antes de firmar valida que exista, que no esté vencido, y que la clave privada corresponda al certificado.
- **Workaround TLS**: los servidores de AFIP usan claves DH débiles; el cliente inyecta un contexto SSL con `SECLEVEL=1` para poder negociar.
- **Numeración siempre remota**: el próximo número de comprobante sale de `FECompUltimoAutorizado` + 1 en cada corrida — nunca se guarda localmente.

## Cómo funciona

```text
python -m factura <proyecto> <pago#>
        │
        ├─ 1. factura-config.yaml  → emisor, certificado, ambiente, datos de pago
        ├─ 2. projects-registry.yaml (read-only) → cliente + plan de pagos
        ├─ 3. invoices.yaml → ¿ya emitido? → sí: "YA EMITIDO", exit 0
        ├─ 4. cotización USD (si aplica): dolarapi → bluelytics → prompt
        │
        ├─ [--dry-run] → PDF "BORRADOR (SIN CAE)" + audit log. Fin.
        │
        └─ [live]
            ├─ WSAA: cache de token → o TRA + firma CMS + loginCms
            ├─ WSFEv1: FEParamGetCotizacion (USD) → FECompUltimoAutorizado
            ├─ FECAESolicitar → CAE (si Resultado != 'A', aborta con las Observaciones)
            ├─ PDF con QR → {proyecto}/docs/facturas/
            ├─ invoices.yaml ← registro de la emisión
            └─ audit.log ← línea EMITIDA
```

El concepto por defecto es `"{descripción del pago o nombre del proyecto} - Pago {n}/{total}"`. Las fechas de servicio (`--desde`/`--hasta`) defaultean a hoy y el vencimiento de pago (`--vto`) a hoy + 10 días. Todos los comprobantes se emiten con Concepto 2 (Servicios), DocTipo 80 (CUIT) e IVA 0 (régimen Monotributo, letra C).

## Instalación

Requiere **Python ≥ 3.11** y **OpenSSL** en el PATH (para generar el certificado).

```bash
git clone https://github.com/oxxo/arca-factura.git
cd arca-factura
pip install -e .
```

Dependencias: `zeep`, `cryptography`, `weasyprint`, `jinja2`, `qrcode[pil]`, `pyyaml`, `requests`. La instalación registra también el script de consola `factura`, equivalente a `python -m factura` solo para la emisión: el subcomando `setup-cert` únicamente funciona vía `python -m factura setup-cert` (el script apunta directo a `factura.cli:main` y saltea el router de `__main__.py` que lo intercepta).

> **WeasyPrint en Windows** necesita las librerías GTK (Pango/Cairo). Si `check_weasyprint` falla, la propia herramienta imprime el hint de instalación — pero ojo: el chequeo corre justo antes de renderizar el PDF, que en modo live es *después* de obtener el CAE (ver la ventana CAE-sin-registrar en Estado). Verificá WeasyPrint con un `--dry-run` antes de emitir en vivo.

### Certificado AFIP (una sola vez)

Los web services de AFIP autentican con certificado X.509, no con usuario y contraseña. El subcomando `setup-cert` automatiza la parte local:

```bash
python -m factura setup-cert
```

Esto:

1. Genera una clave privada RSA-2048 y un CSR en `~/.afip/` (`clave_privada.key`, `solicitud.csr`) con subject `/CN={nombre}/serialNumber=CUIT {cuit}` tomados de `factura-config.yaml`.
2. Imprime el paso a paso para el portal de ARCA: crear el alias de certificado en *Administración de Certificados Digitales*, pegar el CSR, descargar el `.crt` a `~/.afip/certificado.crt`, y vincular el servicio **wsfe** al certificado en *Administrador de Relaciones de Clave Fiscal*.

Volver a correr `setup-cert` con el certificado ya instalado lo valida (vigencia + correspondencia clave/certificado). Todo el material sensible (clave, certificado, cache de token) vive en `~/.afip/`, **fuera del repo**.

## Uso

```bash
# Emitir el pago 3 del proyecto "acme" (ambiente según config)
python -m factura acme 3

# Ensayo: PDF borrador, sin tocar AFIP, sin escribir el ledger
python -m factura acme 3 --dry-run

# Contra homologación (flujo completo, endpoints de prueba)
python -m factura acme 3 --homo

# FCE MiPyMEs con período de servicio y vencimiento explícitos
python -m factura globex 1 --tipo 211 --desde 20260601 --hasta 20260630 --vto 20260715

# Nota de crédito de una factura ya emitida (11→13, 211→213 automático)
python -m factura acme 3 --nc

# Cotización manual (p. ej., MEP pactado) — saltea dolarapi y FEParamGetCotizacion
python -m factura globex 2 --cotizacion 1250.50

# Forzar monto en pesos (ignora la moneda del registro)
python -m factura acme 3 --monto 500000 --concepto-override "Ajuste por alcance adicional"
```

Volver a correr un comando ya emitido no hace nada peligroso:

```text
*** YA EMITIDO ***
Comprobante: 00000042
CAE: 00000000000000
Fecha: 2026-07-01
Archivo: 20123456789_011_00001_00000042.pdf
```

### Flags

| Flag | Descripción |
| ---- | ----------- |
| `proyecto` (posicional) | Slug del proyecto en el registro. Matchea por prefijo de `id` o substring del nombre, case-insensitive; ante empate gana el que tiene bloque `billing`. |
| `pago` (posicional, int) | Número de pago dentro del plan de pagos del proyecto. |
| `--dry-run` | PDF borrador sin CAE, sin conexión a AFIP, sin escritura en el ledger (sí en el audit log). |
| `--homo` | Fuerza el ambiente de homologación (pisa el `ambiente` del config). No hay flag `--prod`: producción se activa desde el config. |
| `--tipo N` | Pisa el tipo de comprobante del registro (11 = Factura C, 211 = FCE MiPyMEs C). Es un entero: `011` equivale a `11`. |
| `--desde` / `--hasta` | Período del servicio, `YYYYMMDD`. Default: hoy. |
| `--vto` | Vencimiento de pago, `YYYYMMDD`. Default: hoy + 10 días. |
| `--concepto-override` | Reemplaza la descripción por defecto del comprobante. |
| `--monto` | Monto en ARS. Fuerza `MonId=PES` y saltea toda la lógica de cotización. |
| `--nc` | Emite la nota de crédito de una factura ya registrada en el ledger. Solo mapea 11→13 y 211→213. |
| `--cotizacion` | Cotización USD→ARS manual. Saltea dolarapi/bluelytics y `FEParamGetCotizacion`. |
| `setup-cert` (subcomando) | Genera clave + CSR e imprime las instrucciones del portal de ARCA. |

## Configuración

Los dos archivos con datos reales (`factura-config.yaml`, `invoices.yaml`) están gitignoreados — arrancá copiando los ejemplos versionados: [`factura-config.example.yaml`](factura-config.example.yaml), [`projects-registry.example.yaml`](projects-registry.example.yaml), [`invoices.example.yaml`](invoices.example.yaml).

### `factura-config.yaml` (datos del emisor)

```yaml
emisor:
  nombre: "Perez Juan"
  cuit: "20123456789"
  condicion_iva: "Responsable Monotributo"
  domicilio: "Ciudad, Provincia"
  punto_venta: 1
  inicio_actividades: "01/01/2020"

certificado:
  key_path: "~/.afip/clave_privada.key"
  crt_path: "~/.afip/certificado.crt"

# homologacion | produccion
ambiente: "homologacion"

# Datos de pago que se imprimen en las FCE (Opcionales 2101/2102)
pago:
  cbu: "0000000000000000000000"
  alias: "MI.ALIAS.CBU"
  condicion: "Transferencia Bancaria"

pdf:
  output_base: ""   # vacío = {path del proyecto}/docs/facturas/

# Registro externo de clientes (solo lectura)
registry_path: "./projects-registry.example.yaml"
```

Si falta la clave `ambiente`, el default es `homologacion`.

### Registro de proyectos (externo, solo lectura)

La herramienta es inutilizable sin este archivo: es la fuente de verdad de clientes y planes de pago. Esquema mínimo por proyecto:

```yaml
projects:
  - id: acme-001
    name: "Acme Corp"
    path: "D:/proyectos/acme"
    billing:
      razon_social: "ACME S.A."
      cuit: "30123456789"
      condicion_iva: "Responsable Inscripto"
      domicilio: "Av. Siempreviva 742, CABA"
      tipo_comprobante: 11        # 11 = Factura C | 211 = FCE MiPyMEs C
      # Para FCE:
      # referencia_comercial: "OC 12345"
      # opcion_transferencia: "SCA"
    financials:
      currency: USD               # USD | ARS
    payments:
      - number: 1
        amount: 1000
        description: "Anticipo"
      - number: 2
        amount: 1000
        description: "Entrega final"
```

### Archivos que genera la herramienta

| Archivo | Qué es |
| ------- | ------ |
| `invoices.yaml` | Ledger de idempotencia. Una entrada por comprobante: número, tipo, CAE y vencimiento, monto, moneda, cotización, concepto, nombre de archivo, fecha de emisión. |
| `factura/audit.log` | Trail append-only de todas las corridas (gitignoreado). |
| `~/.afip/token_cache.json` | Cache del token WSAA (validez ~12 h). Fuera del repo. |
| `{proyecto}/docs/facturas/*.pdf` | Los comprobantes generados (gitignoreados). |

## Estado

Herramienta personal en uso real, publicada como referencia y **as-is**: no es un producto con soporte — los issues y PRs pueden quedar sin respuesta, y ARCA cambia el contrato de sus web services según su propio calendario. Cosas que hay que saber antes de adoptarla:

- **Sin tests automatizados.** El único artefacto de verificación es `factura/spike_cms.py`, un spike manual que valida la firma CMS/PKCS#7 en Python 3.13 + Windows.
- **Sin gate de producción**: con `ambiente: "produccion"` en el config, un comando pelado emite un comprobante fiscal real. Las válvulas de seguridad (`--dry-run`, `--homo`) son opt-in. Recomendación: dejá `homologacion` en el config y cambiá a producción de forma consciente.
- **Ventana de riesgo CAE-sin-registrar**: el PDF y la escritura del ledger ocurren *después* de obtener el CAE. Si el proceso muere en ese medio (p. ej., falla WeasyPrint), el CAE existe en AFIP pero no en `invoices.yaml`, y un reintento emitiría un segundo comprobante. El método de recuperación (`FECompConsultar`) está implementado en `afip_wsfe.py` pero no cableado al CLI — ante la duda, consultá el último autorizado en el portal antes de reintentar.
- **Topes de Monotributo hardcodeados** (categorías H e I, valores aproximados 2025/2026); solo se chequea la categoría H.
- El ledger se reescribe completo en cada emisión, sin lock ni backup: no corras dos emisiones en paralelo.
- Manejo de errores por `sys.exit()`: un error transitorio de AFIP a mitad de flujo aborta la corrida (lo cual, dado el punto anterior sobre el CAE, es el comportamiento conservador correcto).

## Disclaimer

Este proyecto no está afiliado a ARCA/AFIP ni constituye asesoramiento impositivo. Los comprobantes electrónicos son documentos fiscales con efectos legales: verificá tu situación tributaria, categoría y obligaciones con tu contador/a antes de emitir en producción. El software se provee tal cual, sin garantía de ningún tipo; el uso es bajo tu exclusiva responsabilidad.

## Documentación

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — diseño módulo por módulo, flujo de datos, detalles del protocolo AFIP *(en inglés)*
- [docs/BEST-PRACTICES.md](docs/BEST-PRACTICES.md) — guía operativa, gaps conocidos, checklist de hardening *(en inglés)*
- [README.md](README.md) — este documento en inglés

## Licencia

[MIT](LICENSE)
