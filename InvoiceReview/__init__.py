from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import time
import requests
import logging

import azure.functions as func

# --- Constantes y configuración ---
URL_HOLDED_INVOICE = "https://api.holded.com/api/invoicing/v1/documents/invoice"
URL_HOSTAWAY_TOKEN = "https://api.hostaway.com/v1/accessTokens"

SERIE_FACTURACION_DEFAULT = "Alojamientos"
IVA_DEFAULT = Decimal("0.10")

# Mapea nombre de serie -> numSerieId de Holded
PARAMETRO_A_ID = {
    "Rocio": "65d9f06600a829a27305f066",
    "Alojamientos": "65d9f0e90396551d79088219",
    "Efectivo": "62115e5292bee258e53a6756",
}

# Recomendado: mover a variables de entorno en Azure (App Settings / Key Vault)
HOSTAWAY_CLIENT_ID = os.environ.get("HOSTAWAY_CLIENT_ID", "81585")
HOSTAWAY_CLIENT_SECRET = os.environ.get(
    "HOSTAWAY_CLIENT_SECRET",
    "0e3c059dceb6ec1e9ec6d5c6cf4030d9c9b6e5b83d3a70d177cf66838694db5f",
)
HOLDED_API_KEY = os.environ.get("HOLDED_API_KEY", "260f9570fed89b95c28916dee27bc684")

# --- Helper con reintentos/backoff ligero ---
def _request(method, url, *, max_retries=3, backoff_base=1.5, **kwargs):
    for attempt in range(max_retries + 1):
        resp = requests.request(method, url, timeout=30, **kwargs)
        # Reintentar en 429/5xx
        if resp.status_code in (429,) or 500 <= resp.status_code < 600:
            if attempt < max_retries:
                time.sleep(backoff_base ** attempt)
                continue
        # Para cualquier otro código, levanta si es error
        resp.raise_for_status()
        return resp
    # Si sale del bucle sin devolver, última respuesta con error
    resp.raise_for_status()


# --- Auth Hostaway ---
def obtener_acceso_hostaway():
    payload = {
        "grant_type": "client_credentials",
        "client_id": HOSTAWAY_CLIENT_ID,
        "client_secret": HOSTAWAY_CLIENT_SECRET,
        "scope": "general",
    }
    headers = {
        "Content-type": "application/x-www-form-urlencoded",
        "Cache-control": "no-cache",
    }
    r = _request("POST", URL_HOSTAWAY_TOKEN, data=payload, headers=headers)
    return r.json()["access_token"]


# --- Paginación offset/limit de Hostaway ---
def retrieveReservations(arrivalStartDate, arrivalEndDate, token, limit=500, timeout=30, max_pages=200):
    """
    Pagina por offset hasta agotar 'count' o hasta que el bloque sea < limit.
    Si Hostaway capa limit (p.ej. a 100), se adapta leyendo 'limit' efectivo de la respuesta.
    """
    base = (
        "https://api.hostaway.com/v1/reservations"
        f"?arrivalStartDate={arrivalStartDate}"
        f"&arrivalEndDate={arrivalEndDate}"
        f"&includeResources=1"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-type": "application/json",
        "Cache-control": "no-cache",
    }

    all_results = []
    offset = 0
    pages = 0

    # Primera llamada para descubrir count y limit efectivo
    url0 = f"{base}&limit={limit}&offset={offset}"
    resp = _request("GET", url0, headers=headers)
    data = resp.json() or {}
    total = data.get("count")
    eff_limit = data.get("limit") or limit
    chunk = data.get("result") or []
    all_results.extend(chunk)

    # Si no hay más datos, devolvemos
    if total is None:
        # Fallback por longitud de bloque
        while chunk and len(chunk) >= eff_limit and pages < max_pages:
            pages += 1
            offset += eff_limit
            url = f"{base}&limit={eff_limit}&offset={offset}"
            resp = _request("GET", url, headers=headers)
            data = resp.json() or {}
            chunk = data.get("result") or []
            all_results.extend(chunk)
            eff_limit = data.get("limit") or eff_limit
        return {"result": all_results}

    # Con count conocido
    while len(all_results) < total and pages < max_pages:
        pages += 1
        offset += eff_limit
        url = f"{base}&limit={eff_limit}&offset={offset}"
        resp = _request("GET", url, headers=headers)
        data = resp.json() or {}
        chunk = data.get("result") or []
        if not chunk:
            break
        all_results.extend(chunk)
        eff_limit = data.get("limit") or eff_limit
        if len(chunk) < eff_limit:
            break

    return {"result": all_results}


# --- Fechas (start, end) ---
def obtener_fechas():
    start = (datetime.now() - timedelta(weeks=2)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    return start, end


# --- Chequeo si ya está facturada ---
def comprobar_si_existe_factura(reserva):
    for field in (reserva.get("customFieldValues") or []):
        # Busca por nombre del custom field
        if field.get("customField", {}).get("name") == "holdedID":
            return field.get("value") == "Ya esta facturada"
        # O por ID conocido (si lo sabes)
        if field.get("customFieldId") == 56844:
            return field.get("value") == "Ya esta facturada"
    return False


# --- Determinar serie e IVA ---
def determinar_serie_y_iva(reserva, token):
    """
    Regla actual:
      - Si paymentMethod == 'cash' => serie 'Efectivo' e IVA 0 (OJO: valida fiscalmente esta regla).
      - Si customFieldId == 57829 => serie según valor del campo.
      - Serie 'Rocio' => IVA 0.
    """
    serie_facturacion = SERIE_FACTURACION_DEFAULT
    iva = IVA_DEFAULT

    # Lee método de pago (si existe algún cargo)
    reserva_id = str(reserva.get("hostawayReservationId"))
    url = f"https://api.hostaway.com/v1/guestPayments/charges?reservationId={reserva_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-type": "application/json",
        "Cache-control": "no-cache",
    }
    data = _request("GET", url, headers=headers).json() or {}
    result = data.get("result") or []
    payment_method = result[0].get("paymentMethod") if result else None
    if payment_method == "cash":
        serie_facturacion = "Efectivo"
        iva = Decimal("0.00")

    # Campo personalizado de serie (57829)
    for field in (reserva.get("customFieldValues") or []):
        if field.get("customFieldId") == 57829:
            if field.get("value"):
                serie_facturacion = field["value"]

    if serie_facturacion == "Rocio":
        iva = Decimal("0.00")

    return serie_facturacion, iva


# --- Marcar reserva como facturada en Hostaway ---
def marcarComoFacturada(reserva, token):
    try:
        reserva_id = str(reserva.get("hostawayReservationId"))
        url = f"https://api.hostaway.com/v1/reservations/{reserva_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-type": "application/json",
            "Cache-control": "no-cache",
        }

        custom_fields = list(reserva.get("customFieldValues") or [])
        # Busca existente por nombre o por ID
        actualizado = False
        for field in custom_fields:
            if field.get("customField", {}).get("name") == "holdedID" or field.get("customFieldId") == 56844:
                field["value"] = "Ya esta facturada"
                actualizado = True
                break
        if not actualizado:
            custom_fields.append({"customFieldId": 56844, "value": "Ya esta facturada"})

        payload = {"customFieldValues": custom_fields}
        _request("PUT", url, json=payload, headers=headers)
        return "Marcada como facturada."
    except requests.RequestException as e:
        logging.error(f"Error al marcar como facturada: {e}")
        return f"Error al marcar como facturada: {e}"


# --- Crear factura en Holded (timestamp en segundos, como pediste) ---
def crear_factura(reserva, serie_facturacion, iva):
    try:
        now = datetime.now()
        timestamp_seconds = int(now.timestamp())  # NO cambiar a ms
        serie_id = PARAMETRO_A_ID.get(serie_facturacion, PARAMETRO_A_ID[SERIE_FACTURACION_DEFAULT])

        total = Decimal(str(reserva.get("totalPrice", 0)))
        base = (total / (Decimal("1") + iva)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        tax_pct = int((iva * 100).quantize(Decimal("1")))  # 10, 0, etc.

        payload = {
            "applyContactDefaults": True,
            "items": [
                {
                    "tax": tax_pct,
                    "name": f"{reserva.get('listingName', '')} - {reserva.get('arrivalDate', '')} a {reserva.get('departureDate', '')}",
                    "subtotal": str(base),
                }
            ],
            "currency": reserva.get("currency", "EUR"),
            "date": timestamp_seconds,  # segundos
            "numSerieId": serie_id,
            "approveDoc": True,
            "contactName": reserva.get("guestName", "Huésped"),
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "key": HOLDED_API_KEY,
        }
        r = _request("POST", URL_HOLDED_INVOICE, json=payload, headers=headers)
        return r.status_code, r.json()
    except requests.RequestException as e:
        logging.error(f"Error al crear la factura: {e}")
        raise


# --- Entry point Timer Trigger ---
def main(mytimer: func.TimerRequest) -> None:
    access_token = obtener_acceso_hostaway()
    start, end = obtener_fechas()

    reservas_json = retrieveReservations(
        arrivalStartDate=start,
        arrivalEndDate=end,
        token=access_token,
        limit=500,          # sube el límite; si Hostaway lo capa, se adapta
        timeout=30,
        max_pages=200,
    )
    listaReservas = (reservas_json or {}).get("result") or []

    for reserva in listaReservas:
        rid = reserva.get("hostawayReservationId")
        logging.info(f"{rid} - Procesando reserva…")

        if reserva.get("paymentStatus") != "Paid":
            logging.info(f"{rid} - No está pagada aún")
            continue

        if comprobar_si_existe_factura(reserva):
            logging.info(f"{rid} - Ya existe la factura")
            continue

        serie_facturacion, iva = determinar_serie_y_iva(reserva, access_token)

        try:
            status, factura_info = crear_factura(reserva, serie_facturacion, iva)
        except Exception as e:
            logging.error(f"{rid} - Error al crear factura: {e}")
            continue

        if 200 <= status < 300:
            marcarComoFacturada(reserva, access_token)
            logging.info(f"{rid} - Factura generada en Holded y marcada en Hostaway")
        else:
            logging.error(f"{rid} - Error en respuesta de Holded: status={status} info={factura_info}")

    utc_timestamp = datetime.now(timezone.utc)
    logging.info("Python timer trigger function ran at %s", utc_timestamp)