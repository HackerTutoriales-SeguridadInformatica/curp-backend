from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import base64
import os
import time

app = FastAPI()

# La URL de tu servicio de Selenium en Railway (la configuraremos después)
SELENIUM_URL = os.getenv("SELENIUM_URL", "http://localhost:4444")

# Modelo para recibir la CURP y el captcha desde tu panel
class CurpRequest(BaseModel):
    curp: str
    captcha: str

@app.post("/descargar-curp")
async def descargar_curp(payload: CurpRequest):
    """
    Recibe la CURP y el texto del captcha, automatiza la descarga del PDF.
    """
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')  # Ejecutar sin interfaz gráfica
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    
    # Aquí usaremos las capacidades para conectarnos al servicio remoto
    options.set_capability("browserName", "chrome")
    
    driver = None
    try:
        # Conectar al navegador remoto en Railway
        driver = webdriver.Remote(
            command_executor=SELENIUM_URL,
            options=options
        )
        
        # 1. Navegar al portal de la CURP
        driver.get("https://consultas.curp.gob.mx/CurpSP/consulinterCurSP.do")
        
        # 2. Esperar y llenar el campo de la CURP
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.NAME, "curp"))
        ).send_keys(payload.curp)
        
        # 3. Llenar el campo del captcha
        driver.find_element(By.NAME, "captcha").send_keys(payload.captcha)
        
        # 4. Enviar el formulario
        driver.find_element(By.XPATH, "//input[@type='submit']").click()
        
        # 5. Esperar a que cargue la página de resultados y encontrar el botón de descarga del PDF
        time.sleep(2)  # Pequeña espera para que se renderice la página
        boton_pdf = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(@href, 'PDF')]"))
        )
        
        # Obtener la URL del PDF
        url_pdf = boton_pdf.get_attribute('href')
        
        # 6. Descargar el PDF usando requests (más eficiente que a través del navegador)
        session = requests.Session()
        # Transferir cookies del navegador a la sesión de requests
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])
        
        pdf_response = session.get(url_pdf)
        pdf_response.raise_for_status()
        
        # 7. Devolver el PDF como respuesta
        return Response(content=pdf_response.content, media_type="application/pdf")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if driver:
            driver.quit()