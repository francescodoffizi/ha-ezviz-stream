# Ezviz Camera Proxy — Documentazione

## Cosa fa questo Add-on

L'add-on **Ezviz Camera Proxy** fornisce l'integrazione con Home Assistant per le telecamere Ezviz che **non supportano RTSP o LAN Live View**, in particolare lo [Spioncino Ezviz CS-HP2](https://www.ezviz.com/product/cs-hp2/7952).

Poiché l'HP2 comunica esclusivamente tramite il Cloud Ezviz (protocollo proprietario P2P), questo add-on:

1. **Si autentica con l'API Ezviz Cloud** usando la libreria `pyezvizapi`.
2. **Scarica periodicamente le istantanee (snapshot)** dal cloud e le salva localmente.
3. **Espone endpoint HTTP** per ottenere l'istantanea JPEG, la simulazione del flusso MJPEG, lo stato del dispositivo e gli eventi di allarme.
4. **Fornisce una Web UI integrata** accessibile tramite Home Assistant Ingress (pannello laterale).
5. **Invia eventi MQTT** istantanei in caso di rilevamento di movimento o pressione del campanello.

---

## Perché questo approccio?

Ezviz ha deliberatamente disabilitato il supporto RTSP e LAN Live View sullo spioncino HP2. Qualsiasi tentativo di connettersi ad indirizzi RTSP locali (`rtsp://admin:<pwd>@<ip>:554/...`) fallirà. L'unico percorso di accesso supportato è:

- **App ufficiale Ezviz** (utilizza il protocollo proprietario P2P)
- **API Cloud Ezviz** (tramite la libreria Python `pyezvizapi` o REST API)
- **Ezviz Open Platform** (per flussi sviluppatori HLS/RTMP, richiede un account sviluppatore a pagamento)

Questo add-on utilizza l'approccio API Cloud (pyezvizapi), che è gratuito e funziona con qualsiasi account Ezviz standard.

---

## Installazione

### Passaggio 1: Aggiungi il Repository

Su Home Assistant, vai su **Impostazioni → Add-on → Raccolta di Add-on → Menù (tre puntini) → Repository** e inserisci:

```
https://github.com/francescodoffizi/ha-ezviz-stream
```

### Passaggio 2: Installa l'Add-on

Cerca "Ezviz Camera Proxy" nel negozio degli add-on e premi **Installa**.

### Passaggio 3: Configurazione

Modifica la configurazione dell'add-on:

| Opzione | Richiesto | Predefinito | Descrizione |
|--------|----------|---------|-------------|
| `ezviz_username` | Sì | — | Email dell'account Ezviz |
| `ezviz_password` | Sì | — | Password dell'account Ezviz |
| `ezviz_region` | No | `apiieu.ezvizlife.com` | Regione dell'API (vedi sotto) |
| `camera_serial` | Sì | — | Numero di serie della telecamera (9 caratteri) |
| `camera_password` | Sì | — | Codice di verifica della telecamera (etichetta sul dispositivo) |
| `snapshot_interval` | No | `30` | Secondi tra gli snapshot cloud (5–300) |
| `enable_mqtt_events` | No | `true` | Pubblica gli eventi su HA MQTT |

### Regioni API

| Regione | Endpoint |
|--------|----------|
| Europa (predefinito) | `apiieu.ezvizlife.com` |
| Nord America | `apiusa.ezvizlife.com` |
| Cina | `api.ezvizlife.com` |
| Resto del Mondo | `apiglobal.ezvizlife.com` |

### Passaggio 4: Avvia l'Add-on

Premi **Avvia**. L'add-on avvia un server web sulla porta 8099 accessibile tramite HA Ingress.

---

## Web UI

Una volta avviato, apri il pannello **Ezviz Camera** nella barra laterale di HA. La dashboard mostra:

- Istantanea live (aggiornata automaticamente ogni `snapshot_interval` secondi)
- Pulsante di aggiornamento manuale
- Stato della fotocamera: online/offline, livello della batteria, segnale Wi-Fi, versione del firmware
- Eventi di allarme recenti con tipo, timestamp e link locale all'immagine dell'evento
- Link agli endpoint delle API JSON

---

## Utilizzo come Entità Telecamera in Home Assistant

### Telecamera Generica (Istantanea)

Aggiungi questo al tuo `configuration.yaml`:

```yaml
camera:
  - platform: generic
    name: "Spioncino HP2 (Istantanea)"
    still_image_url: "http://localhost:8099/api/snapshot"
    verify_ssl: false
    scan_interval: 30
```

### Telecamera Generica (Simulazione Stream MJPEG)

Per simulare un flusso live:

```yaml
camera:
  - platform: generic
    name: "Spioncino HP2 (Stream)"
    still_image_url: "http://localhost:8099/api/snapshot"
    stream_source: "http://localhost:8099/api/stream"
    verify_ssl: false
```

> **Nota:** Il flusso MJPEG è simulato — riproduce a ciclo continuo i fotogrammi storici e gli snapshot salvati.
> Un vero video in tempo reale non è possibile senza l'uso dell'API a pagamento Ezviz Open Platform.

### Sensori REST per lo Stato della Telecamera

```yaml
sensor:
  - platform: rest
    name: "Spioncino Batteria"
    resource: "http://localhost:8099/api/status"
    value_template: "{{ value_json.battery_level }}"
    unit_of_measurement: "%"
    device_class: battery
    scan_interval: 300

  - platform: rest
    name: "Spioncino Online"
    resource: "http://localhost:8099/api/status"
    value_template: "{{ value_json.online }}"
    scan_interval: 60
```

### Automazione del Campanello tramite Eventi MQTT

Quando `enable_mqtt_events: true` è attivo, l'add-on pubblica messaggi su:

- `homeassistant/camera/ezviz/<serial>/doorbell` — Quando viene premuto il campanello
- `homeassistant/camera/ezviz/<serial>/motion`   — Quando viene rilevato un movimento

Esempio di automazione:

```yaml
automation:
  - alias: "Notifica Campanello HP2"
    trigger:
      - platform: mqtt
        topic: "homeassistant/camera/ezviz/AB1234567/doorbell"
    action:
      - service: notify.mobile_app_tuo_telefono
        data:
          message: "Qualcuno ha suonato alla porta!"
          data:
            image: "/api/camera_proxy/camera.spioncino_hp2_stream"
```

---

## Riferimento API

| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/` | GET | Dashboard Web |
| `/api/snapshot` | GET | Istantanea JPEG corrente |
| `/api/snapshot/refresh` | GET/POST | Forza un nuovo snapshot dal Cloud |
| `/api/status` | GET | Stato fotocamera in formato JSON |
| `/api/events` | GET | Eventi recenti in formato JSON |
| `/api/stream` | GET | Flusso MJPEG simulato |
| `/api/devices` | GET | Tutti i dispositivi dell'account |
| `/api/health` | GET | Controllo di stato dell'add-on |

---

## Risoluzione dei Problemi

### "Auth error: Login failed"

- Verifica attentamente l'username e la password di Ezviz.
- Se hai modificato la password di recente, aggiornala nella configurazione dell'add-on.
- Se hai l'autenticazione a due fattori attiva su Ezviz, potrebbe essere necessario confermare l'accesso dall'app mobile durante la prima esecuzione dell'add-on.

### "Could not load camera: …"

- Verifica il numero di serie (codice a 9 caratteri, es: "AB1234567").
- Verifica il codice di verifica della fotocamera (codice di 6 lettere maiuscole sull'etichetta del dispositivo, **non** la password del tuo account).
- Assicurati che lo spioncino sia registrato sullo stesso account Ezviz configurato nell'add-on.

### "Snapshot returned empty data"

- Lo spioncino HP2 è alimentato a batteria ed entra in sonno profondo (deep sleep) tra un evento e l'altro.
- Risveglia lo spioncino premendo il campanello o camminandoci davanti.
- Aumenta il valore di `snapshot_interval` per ridurre il consumo di batteria — l'HP2 si attiva ogni volta che viene richiesto uno snapshot dal cloud.

### La foto non si aggiorna mai

- Controlla i log dell'add-on: **Impostazioni → Add-on → Ezviz Camera Proxy → Log**.
- Assicurati che le credenziali siano corrette e che l'host abbia accesso ad Internet.

### La batteria si scarica velocemente

- Aumenta il `snapshot_interval` (es: 60 o 120 secondi), oppure impostalo su `0` (disattivato) per attivare la modalità **risparmio energetico**.
- Con intervallo `0`, il proxy risveglierà la fotocamera per scattare una foto **solo** quando riceve un evento push reale (movimento/campanello), salvaguardando enormemente la carica.

---

## Risorse Utili

- [pyezvizapi su PyPI](https://pypi.org/project/pyezvizapi/)
- [Integrazione ufficiale Ezviz in HA](https://www.home-assistant.io/integrations/ezviz/)
- [Documentazione Add-on di Home Assistant](https://developers.home-assistant.io/docs/add-ons/configuration)
- [Repository GitHub di questo Add-on](https://github.com/francescodoffizi/ha-ezviz-stream)
