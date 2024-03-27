from datetime import datetime,timedelta,timezone
import requests
import logging

import azure.functions as func

URL_HOLDED_INVOICE = "https://api.holded.com/api/invoicing/v1/documents/invoice"
URL_HOSTAWAY_TOKEN = "https://api.hostaway.com/v1/accessTokens"
SERIE_FACTURACION_DEFAULT = "Alojamientos"
IVA_DEFAULT = 0.21
PARAMETRO_A_ID = {
    "Rocio": "65d9f06600a829a27305f066",
    "Alojamientos": "65d9f0e90396551d79088219",
    "Efectivo": "62115e5292bee258e53a6756",
}

def obtener_acceso_hostaway():
    try:
        payload = {
            "grant_type": "client_credentials",
            "client_id": "81585",
            "client_secret": "0e3c059dceb6ec1e9ec6d5c6cf4030d9c9b6e5b83d3a70d177cf66838694db5f",
            "scope": "general"
        }
        headers = {'Content-type': "application/x-www-form-urlencoded", 'Cache-control': "no-cache"}
        response = requests.post(URL_HOSTAWAY_TOKEN, data=payload, headers=headers)
        response.raise_for_status()
        return response.json()["access_token"]
    except requests.RequestException as e:
        logging.error(f"Error al obtener el token de acceso: {str(e)}")
        raise

def retrieveReservations(arrivalStartDate, arrivalEndDate,token):
    
    url = f"https://api.hostaway.com/v1/reservations?arrivalStartDate={arrivalStartDate}&arrivalEndDate={arrivalEndDate}&includeResources=1" 

    headers = {
        'Authorization': f"Bearer {token}",
        'Content-type': "application/json",
        'Cache-control': "no-cache",
    }

    response = requests.get(url, headers=headers)
    data = response.json()
    return data
def obtener_fechas():
    fecha_actual = datetime.now()
    fecha_hace_dos_semanas = fecha_actual - timedelta(weeks=2)
    return fecha_actual.strftime('%Y-%m-%d'), fecha_hace_dos_semanas.strftime('%Y-%m-%d')

def comprobar_si_existe_factura(reserva):
    custom_fields = reserva["customFieldValues"]
    for field in custom_fields:
        if field["customField"]["name"] == "holdedID":
            if field["value"] == "Ya esta facturada":
                return True
    return False

def determinar_serie_y_iva(reserva,token):

    serie_facturacion = SERIE_FACTURACION_DEFAULT
    iva = IVA_DEFAULT
    reserva_id = str(reserva["hostawayReservationId"])
    url = f" https://api.hostaway.com/v1/guestPayments/charges?reservationId={reserva_id}"
    headers = {
            'Authorization': f"Bearer {token}",
            'Content-type': "application/json",
            'Cache-control': "no-cache",
        }
    response = requests.get(url, headers=headers)
    data = response.json()

    # Acceder al 'paymentMethod' del primer elemento de 'result'
    payment_method = data['result'][0]['paymentMethod']
    if payment_method == "cash":
        serie_facturacion="Efectivo"
        iva=0
        return serie_facturacion,iva
    
    custom_fields = reserva.get("listingCustomFields", [])
    for field in custom_fields:
        if field["customField"]["name"] == "Serie_Facturación":
            serie_facturacion = field["value"]
        if serie_facturacion == "Rocio":
            iva = 0
            break

    return serie_facturacion, iva
            
def marcarComoFacturada(reserva,token):
    encontrado=False
    try:
        
        reserva_id = str(reserva["hostawayReservationId"])
        url = f"https://api.hostaway.com/v1/reservations/{reserva_id}"
        headers = {
            'Authorization': f"Bearer {token}",
            'Content-type': "application/json",
            'Cache-control': "no-cache",
        }

        custom_fields = reserva["customFieldValues"]
        for field in custom_fields:
            if field["customField"]["name"] == "holdedID":
                field["value"] = "Ya esta facturada"
                encontrado = True  
                break
        if not encontrado:
            nuevoCustomField= {"customFieldValues": [
        {
            "customFieldId": 56844,
            "value": "Ya esta facturada"
        } ]
        }
            response = requests.put(url, json=nuevoCustomField, headers=headers)
        else:
            response = requests.put(url, json=reserva, headers=headers)
        response.raise_for_status()  # Esto lanzará un error si el código de estado es >= 400
        return "Marca como facturada exitosamente."
    except requests.RequestException as e:
        error_msg = f"Error al marcar como facturada: {e}"
        logging.error(error_msg)
        return error_msg
        

def crear_factura(reserva, serie_facturacion, iva):
    try:
        now = datetime.datetime.now()
        timestamp = int(now.timestamp())
        serie_id = PARAMETRO_A_ID.get(serie_facturacion, PARAMETRO_A_ID[SERIE_FACTURACION_DEFAULT])
        payload = {
            "applyContactDefaults": True,
            "items": [{
                "tax": iva * 100,
                "name": f"{reserva['listingName']} - {reserva['arrivalDate']} a {reserva['departureDate']}",
                "subtotal": str(reserva["totalPrice"] / (1 + iva))
            }],
            "currency": reserva["currency"],
            "date": timestamp,
            "numSerieId": serie_id,
            "contactName": reserva["guestName"]
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "key": "260f9570fed89b95c28916dee27bc684"
        }
        response = requests.post(URL_HOLDED_INVOICE, json=payload, headers=headers)
        response.raise_for_status()
        return response.status_code, response.json()
    except requests.RequestException as e:
        logging.error(f"Error al crear la factura: {str(e)}")
        raise

def main(mytimer: func.TimerRequest) -> None:
    access_token = obtener_acceso_hostaway()
    principio, final = obtener_fechas()
    listaReservas = retrieveReservations(arrivalStartDate=final,arrivalEndDate=principio,token=access_token).get("result")
    for reserva in listaReservas:
        if reserva.get("paymentStatus") != "Paid":
            continue
        if comprobar_si_existe_factura(reserva):
            continue
        serie_facturacion, iva = determinar_serie_y_iva(reserva,access_token)
        resultado_crear_factura, factura_info = crear_factura(reserva, serie_facturacion, iva)
        marcarComoFacturada(reserva, access_token)
        

    

    utc_timestamp = datetime.now(timezone.utc)

    logging.info('Python timer trigger function ran at %s', utc_timestamp)
