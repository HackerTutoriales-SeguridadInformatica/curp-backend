@app.post("/descargar-curp")
async def descargar_curp(payload: CurpRequest):
    limpiar_sesiones_viejas()
    curp = payload.curp.strip().upper()
    captcha = payload.captcha.strip().upper()

    if payload.session_id not in sesiones_activas:
        raise HTTPException(status_code=400, detail="La sesión ha expirado. Vuelve a solicitar el CAPTCHA.")

    info = sesiones_activas[payload.session_id]
    driver = info["driver"]

    try:
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # ✅ CORREGIDO: Usar el formulario ejemploForma2 específicamente
        driver.execute_script("""
            document.ejemploForma2.strCurp.value = arguments[0];
            document.ejemploForma2.codigo.value = arguments[1];
        """, curp, captcha)

        time.sleep(1)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # ✅ Enviar el formulario directamente
        driver.execute_script("document.ejemploForma2.submit();")
        time.sleep(5)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # === DIAGNÓSTICO ===
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:800]
            print(f"[DIAGNÓSTICO] URL actual: {driver.current_url}")
            print(f"[DIAGNÓSTICO] Texto de la página: {body_text}")
        except Exception as e:
            print(f"[DIAGNÓSTICO] Error al leer body: {e}")

        # === Buscar enlaces candidatos a PDF ===
        enlaces_candidatos = driver.find_elements(By.XPATH,
            "//a[contains(@href, '.pdf') or contains(@href, 'pdf') "
            "or contains(@href, 'Imprimir') or contains(@href, 'imprimir') "
            "or contains(@href, 'Constancia') or contains(@href, 'constancia') "
            "or contains(@onclick, 'pdf') or contains(@onclick, 'PDF') "
            "or contains(@onclick, 'imprimir') or contains(@onclick, 'Imprimir') "
            "or contains(@href, 'consultaCURP') or contains(@href, 'ConsultaCURP')]"
        )

        print(f"[DIAGNÓSTICO] Enlaces encontrados: {len(enlaces_candidatos)}")
        for i, enlace in enumerate(enlaces_candidatos):
            try:
                print(f"  [{i}] texto='{enlace.text}' href='{enlace.get_attribute('href')}' onclick='{enlace.get_attribute('onclick')}'")
            except:
                pass

        if not enlaces_candidatos:
            raise HTTPException(
                status_code=400,
                detail="No se encontró ningún enlace de descarga. Texto de la página: " + body_text[:400]
            )

        # Tomar el primer enlace con href
        url_pdf = None
        for enlace in enlaces_candidatos:
            href = enlace.get_attribute('href')
            if href and href != "javascript:void(0)" and not href.startswith("javascript:"):
                url_pdf = href
                break

        if not url_pdf:
            # Intentar hacer clic y capturar la nueva URL
            enlaces_candidatos[0].click()
            time.sleep(3)
            url_pdf = driver.current_url
            print(f"[DIAGNÓSTICO] URL tras clic: {url_pdf}")

        print(f"[DIAGNÓSTICO] URL final del PDF: {url_pdf}")

        session = requests.Session()
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])

        pdf_response = session.get(url_pdf, timeout=30)
        pdf_response.raise_for_status()

        contenido = pdf_response.content
        content_type = pdf_response.headers.get('Content-Type', '')

        es_pdf = contenido[:4] == b'%PDF'

        print(f"[DIAGNÓSTICO] Content-Type: {content_type}")
        print(f"[DIAGNÓSTICO] Es PDF válido: {es_pdf}")
        print(f"[DIAGNÓSTICO] Tamaño: {len(contenido)} bytes")
        print(f"[DIAGNÓSTICO] Primeros 300 bytes: {contenido[:300]}")

        try:
            driver.quit()
        except:
            pass
        del sesiones_activas[payload.session_id]

        if not es_pdf:
            texto_recibido = contenido.decode('utf-8', errors='replace')[:500]
            raise HTTPException(
                status_code=500,
                detail=f"El archivo descargado NO es un PDF (Content-Type: {content_type}). Primeros 500 caracteres: {texto_recibido}"
            )

        return Response(
            content=contenido,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=CURP_{curp}.pdf"}
        )

    except HTTPException:
        raise
    except Exception as e:
        try:
            driver.quit()
        except:
            pass
        if payload.session_id in sesiones_activas:
            del sesiones_activas[payload.session_id]
        raise HTTPException(status_code=500, detail=f"Error al descargar CURP: {str(e)}")
