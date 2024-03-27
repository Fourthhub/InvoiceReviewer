from datetime import datetime,timedelta
import requests
import logging

import azure.functions as func
from azure.storage.queue import QueueServiceClient, QueueClient, QueueMessage, BinaryBase64DecodePolicy, BinaryBase64EncodePolicy


URL_HOSTAWAY_TOKEN = "https://api.hostaway.com/v1/accessTokens"
connect_str = "DefaultEndpointsProtocol=https;AccountName=facturaciononcola;AccountKey=ipAS4lsYSlLmk1vhy5L//l2zoXSV2Fui5f0rc3b5ikPzY7SHJvu1w66Rb2h4vZODIxZcddyZnBg3+AStslU+3w==;EndpointSuffix=core.windows.net"
queue_name = "colita"

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

def retrieveReservations(arrivalStartDate, departureStartDate):
    
    token = obtener_acceso_hostaway()
    url = f"https://api.hostaway.com/v1/reservations?arrivalStartDate={arrivalStartDate}&arrivalEndDate={departureStartDate}&includeResources=1" 
    
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
                return False
    return True


def main(mytimer: func.TimerRequest) -> None:
    principio, final = obtener_fechas()
    listaReservas = retrieveReservations(principio,final).get("result")
    
    for reserva1 in listaReservas:
        reserva = reserva1.get("data", {})
        if reserva.get("paymentStatus") != "Paid":
            pass
        if comprobar_si_existe_factura(reserva):
            pass
        else:
            queue_client = QueueClient.from_connection_string(connect_str, queue_name)
            queue_client.send_message(reserva)


    logging.info('Python timer trigger function ran at %s', utc_timestamp)
