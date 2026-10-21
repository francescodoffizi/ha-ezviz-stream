# Changelog

All notable changes to the Ezviz Camera Proxy add-on will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.8.5] — 2026-10-21

### Added & Improved
- **Paginazione Eventi Recenti (`server.py`, `templates/index.html`)**:
  - Aggiunto supporto a paginazione con controlli intuitivi nella dashboard (*Precedente / Successiva*, contatore elementi e indicatore pagina).
  - Estesa l'API `/api/events` con i parametri query `page` e `limit` restituendo metadati (`total`, `total_pages`, `page`, `limit`).
- **Sistema di Conservazione Configurabile (Retention Days) (`config.yaml`, `run.sh`, `server.py`, `translations/en.yaml`)**:
  - Introdotto parametro `video_retention_days` (default `7` giorni, `0` = illimitata) e `max_stored_events` (default `100`).
  - Implementato thread periodico di retention (`_retention_worker`) ogni ora per l'eliminazione automatica di snapshot e registrazioni MP4 più vecchi del periodo impostato.
  - Aggiunto pulsante rapido *"Pulisci Ora 🧹"* in testata eventi per forzare la pulizia immediata su richiesta tramite `POST /api/events/cleanup`.
- **Eliminazione Manuale Singolo Evento (`server.py`, `templates/index.html`)**:
  - Aggiunto pulsante di cancellazione puntuale per singola riga evento con conferma e rimozione contestuale sia dal buffer in memoria sia dai file fisici JPG/MP4 su disco via `DELETE /api/events/<alarm_id>`.

---

## [1.8.4] — 2026-10-21

### Improved
- **Ottimizzazione Responsive UI Mobile (`templates/index.html`)**:
  - Compattati i pulsanti di controllo della testata *Vista Live* (`Aggiorna`, `Live Nativo HLS`, `Riproduci Cronologia`): ridotto padding/font su mobile (`sm`), abbreviate le etichette per evitare il wrapping multilinea sgradevole e aggiunti attributi `title` esplicativi.
  - Ristrutturata la vista degli *Eventi Recenti*: su viewport mobile (`< sm`) il layout passa da riga rigida a flex-column responsivo con azioni allineate ordinatamente a destra su riga dedicata, evitando collisioni di colonne e testi schiacciati.
  - Allineata la coerenza grafica sia sul template Jinja2 renderizzato lato server sia nell'aggiornamento dinamico client-side (`pollEvents`).

---

## [1.8.3] — 2026-10-20

### Fixed & Improved
- **Normalizzazione Etichette Riconoscimento Facciale (`<Nome>appeared` -> `<Nome> appeared`) (`ezviz_client.py`, `server.py`, `templates/index.html`)**:
  - Risolto il problema per cui gli eventi di rilevamento con nome riconosciuto dal cloud Ezviz apparivano attaccati senza spazio (es. `"Lauraappeared"`, `"Francescoappeared"`).
  - Aggiunta la funzione `_normalize_alarm_label()` in `ezviz_client.py` e `server.py` (oltre al fallback client-side in `t()`) e preservato il nome riconosciuto durante il merge degli eventi senza sovrascriverlo con il generico `"Face Recognition"` (`sub_type == "12663"`).
- **Pulizia UI Dashboard (`templates/index.html`)**:
  - Rimossa la dicitura superflua `"Aggiorno in: Solo Eventis"` dalla barra degli strumenti di *Vista Live*.
  - Risolto il bug in `pollStatus()` per cui l'indicatore in alto a destra aggiornava il colore del testo in verde senza mai sostituire la scritta segnaposto `"Connessione in corso…"` con lo stato reale (`Online` / `Offline`), allineando anche il rendering SSR iniziale.
  - Rimosso il badge segnaposto `"Local"` / `"Locale"` dalle righe di *Eventi Recenti* e uniformata l'etichetta del pulsante immagine a `Vedi`.

---

## [1.8.2] — 2026-10-19

### Fixed & Improved
- **Affidabilità Notifiche Movimento e Riconnessione Push MQTT (`server.py`)**:
  - **Root Cause**:
    1. Quando `snapshot_interval` è impostato a 0, il worker in background dormiva 300 secondi (5 minuti). In caso di socket push TCP in stallo o mancata ricezione dal push broker di Francoforte (`ieuopen.ezvizlife.com`), le notifiche di movimento restavano silenti fino al polling successivo.
    2. Il loop di polling del worker scaricava gli allarmi dall'API Ezviz Cloud (`get_alarm_list`) memorizzandoli in `EventStore`, ma **non pubblicava alcun messaggio MQTT verso Home Assistant**. Solo il callback `_on_ezviz_push_message` effettuava la pubblicazione su `homeassistant/binary_sensor/ezviz_<serial>_motion/state`.
    3. Il socket push `ezviz_mqtt` non eseguiva health check né riconnessione automatica se la sessione veniva chiusa dal broker.
  - **Soluzione**:
    1. Estratta la logica di pubblicazione MQTT in `publish_alarm_to_ha_mqtt()` condivisa e thread-safe.
    2. Implementato **fallback ibrido polling $\leftrightarrow$ MQTT**: nel loop del worker (eseguito ogni 20 secondi anche con `snapshot_interval: 0` senza risvegliare la telecamera), se viene rilevato un evento recente ($\le 90\text{s}$) non ancora notificato da push MQTT, viene pubblicato istantaneamente lo stato `ON`/`OFF` e gli attributi su Home Assistant.
    3. Aggiunto controllo continuo di connessione e riconnessione automatica (`ezviz_mqtt.connect()`) nel worker per il client MQTT push di Ezviz.
    4. Sincronizzato l'accesso e la deduplica a `_seen_events` con `threading.Lock`.

---

## [1.8.1] — 2026-10-19

### Fixed
- **Rilevamento Eventi Campanello (`DoorBellPushAlarm` `channel_type=14` e `AlarmType=2701`) (`vendor/pyezvizapi/mqtt.py`, `ezviz_client.py`, `server.py`)**:
  - **Root Cause (da Reverse Engineering APK Ezviz `AnalyzePushMessageManager.java` & `AlarmType.java`)**:
    1. Quando viene premuto il pulsante del campanello sullo spioncino CS-HP2, il push MQTT usa il tracciato CSV `DoorBellPushAlarm` (`ext_parts[0] == "14"`), dove `ext_parts[4]` è `callingType` (`1`), `ext_parts[5]` è `callingId`, `ext_parts[6]` è `callingStatus` (`1`=squillo in arrivo, `2`=risposto, `3`=terminato/perso) e `ext_parts[7]` è `picUrl`. `pyezvizapi` decodificava tutti i messaggi con il tracciato degli allarmi standard (`channel_type == 1`), interpretando `callingType=1` come `alert_type_code=1` (classificato come `motion` anziché `doorbell`) e ignorando l'URL della foto in `ext_parts[7]`.
    2. Il codice ufficiale Ezviz per lo squillo campanello (`DOORBELL_RING = 2701` e `PHONE_CALLS = 20007`) non era presente nella mappatura di `server.py` né preservato come `subType` in `ezviz_client.py` (`get_alarm_list`).
  - **Soluzione**:
    - Aggiunto il parser dedicato per `channel_type == "14"` (`DoorBellPushAlarm`) in `vendor/pyezvizapi/mqtt.py` con estrazione di `calling_id`, `calling_status`, `picUrl` (`ext_parts[7]`), normalizzazione data (`+` -> spazio) e mappatura a `alert_type_code = 2701`.
    - In `_on_ezviz_push_message` (`server.py`), filtrati i pacchetti di fine chiamata (`calling_status != 1`), mappati correttamente `channel_type == 14` e i codici `2701`/`20007`/`10054`/`10055` sul sensore `binary_sensor.ezviz_<serial>_doorbell`, e aggiornato `_last_doorbell_time`.
    - In `get_alarm_list` (`ezviz_client.py`) e `EventStore.add_events` (`server.py`), preservato `subType="2701"` e interrogato anche `s_type="2701"` su `v3/unifiedmsg/list` per non perdere mai le chiamate campanello nello storico.

---

## [1.8.0] — 2026-10-19

### Added & Improved
- **Estrazione Registrazione Autonoma CloudPlay/MicroSD con Pre-Buffer Evento (`ezviz_client.py`, `server.py`)**:
  - **Causa del ritardo nei video evento**: In precedenza `generate_clip()` catturava il flusso Live HLS (`/api/lapp/v2/live/address/get` con `protocol=2`), che richiede 10–25 secondi per risvegliare lo spioncino CS-HP2 dal deep sleep e negoziare lo stream P2P, registrando solo il vuoto post-evento quando il soggetto era già passato. Inoltre `protocol=2` (HLS) su Ezviz OpenAPI ignora i parametri `type=2`/`type=3` e `startTime`/`stopTime`, restituendo sempre `/v3/openlive/`.
  - **Soluzione**:
    1. Aggiunto `get_event_record_info(alarm_time_str)` in `ezviz_client.py` che interroga `/api/lapp/video/by/time` (`recType=1` CloudPlay prioritario, `recType=2` MicroSD fallback) per individuare l'intervallo esatto (`startTime` -> `endTime`, inclusi i 5s di pre-buffer) registrato autonomamente dallo spioncino.
    2. Aggiunto `get_playback_stream_url()` in `ezviz_client.py` che richiede l'endpoint `/api/lapp/v2/live/address/get` con `protocol=3` (RTMP) o `protocol=4` (HTTP-FLV) e `type=3` (CloudPlay) / `type=2` (MicroSD), ottenendo il vero flusso OpenPlayback (`/v3/openpb/...`).
    3. Aggiornato `generate_clip(alarm_id)` in `server.py`: attende il completamento della registrazione autonoma di 10–16s della telecamera, scarica il flusso di playback `/v3/openpb/` normalizzandolo a `1280x720` (`libx264`, `yuv420p`, `+faststart`) per piena compatibilità iOS/Safari (poiché lo stream CloudPlay CS-HP2 cambia risoluzione SPS/PPS da `3712x2160` a `1280x720` dopo il pre-buffer), e mantiene il fallback sul flusso Live HLS qualora non risulti alcuna registrazione indicizzata.

### Fixed & Improved
- **Riproduzione e Download Video Eventi Reali — Stop Redirect alla Vista Live (`server.py`, `templates/index.html`)**:
  - **Root Cause**: L'API Ezviz Open Platform (`/api/lapp/v2/live/address/get` con `protocol=2` HLS) per lo spioncino a batteria CS-HP2 ignora `type=2` e `startTime`/`stopTime`, restituendo sempre l'URL del flusso **Live** (`/v3/openlive/...`). Di conseguenza:
    1. L'endpoint `/api/events/video/<alarm_id>` e il player `/player?alarm_id=...` aprivano sempre la vista Live invece del file `/data/videos/video_<alarm_id>.mp4` registrato su disco.
    2. Il callback push usava l'ID numerico `msg["id"]` (`211186...`) mentre il polling cloud usava `ext["msgId"]` (`MVE...`), disallineando il nome dei file `.mp4` registrati durante l'evento rispetto alla lista eventi.
    3. `generate_clip()` registrava 45s dal flusso Live anche quando veniva invocata ore dopo l'evento (es. al riavvio dell'add-on o al click su `MP4`), producendo clip con il timestamp sbagliato.
  - **Soluzione**:
    - Unificato l'`alarm_id` tra push (`ext["msgId"]`) e polling cloud (`MVE...`), con riconciliazione automatica su disco delle clip genuine registrate con ID numerico e rimozione automatica delle clip postume registrate dal flusso Live (`mtime - alarm_time > 180s`).
    - Avvio immediato della cattura `ffmpeg` (30s con lock per-evento e pre-check dei segmenti `.ts`) all'arrivo del push, bloccando rigorosamente qualsiasi generazione postuma per eventi più vecchi di 120 secondi.
    - `/api/events/video/<alarm_id>`, `/api/events/clip/<alarm_id>` e `/player?alarm_id=<alarm_id>` servono e riproducono esclusivamente il file MP4 locale dell'evento (senza mai reindirizzare al flusso Live); nella dashboard i pulsanti `Video` e `MP4` compaiono solo per gli eventi dotati di registrazione reale (`Solo Foto` per gli altri).
- **Sincronizzazione Immediata Immagine Evento Push Prima della Notifica MQTT (`server.py`)**:
  - Risolto il bug in `_on_ezviz_push_message` per cui l'URL dell'immagine allegata al push real-time Ezviz (`ext["image"]`) veniva ignorato perché cercato solo al livello radice (`msg.get("image")`), ritardando il download di `event_<id>.jpg` di oltre 10 secondi e facendo inviare a Home Assistant l'immagine dell'evento precedente.
  - Il download dell'immagine dell'evento da `ext["image"]` (con fino a 4 tentativi rapidi) e il salvataggio sia su `/data/snapshots/events/event_<id>.jpg` (`local_pic = True`) sia su `/data/snapshots/current.jpg` (`/api/snapshot`) avvengono ora **prima** della pubblicazione di `ON` sul topic MQTT di Home Assistant.
  - In `EventStore`, lo stato `local_pic` viene verificato anche su disco e preservato durante il merge degli eventi, aggiornando `current.jpg` quando viene scaricata l'immagine dell'evento più recente.

---

## [1.7.9] — 2026-10-12

### Fixed & Improved
- **Risolto `import requests` in server.py**: Ripristinato l'import di `requests` nel ciclo di polling dell'endpoint `/api/live/stream.m3u8` che causava l'eccezione `NameError: name 'requests' is not defined` durante la verifica del risveglio telecamera.

---

## [1.7.8] — 2026-10-12

### Fixed & Improved
- **Persistenza Sessione Cloud Ezviz (`cloud_session.json`)**: Risolto il problema per cui a ogni riavvio dell'add-on veniva generata una nuova coppia di chiavi d'accesso/dispositivo registrato sull'app Ezviz. La sessione utente (`sessionId` e `rfSessionId`) ora viene salvata in modo persistente su `/data/cloud_session.json` e riutilizzata tramite `refreshSessionId`, evitando nuovi login completi da zero.

---

## [1.7.7] — 2026-10-12

### Fixed & Improved
- **Finestra di Wake-Up Estesa (25s)**: Aumentata la soglia di tolleranza di risveglio della telecamera HP2 da deep sleep fino a 25 secondi nell'endpoint `/api/live/stream.m3u8`. Il redirect verso Home Assistant viene rilasciato rigorosamente solo quando i segmenti video reali `.ts` sono pronti e riproducibili, impedendo ad Home Assistant di andare in timeout precoce.
- **Log Dettagliati di Stato Risveglio**: Aggiunto tracciamento al secondo del ciclo di wake-up con dimensione della playlist generata.

---

## [1.7.6] — 2026-10-12

### Fixed & Improved
- **Robust Wake-Up Handling per Stream Live (HP2)**: Eliminato definitivamente il fallimento di avvio dello stream in Home Assistant (che richiedeva stop/play manuale). L'endpoint `/api/live/stream.m3u8` ora filtra rigorosamente sia i segmenti di errore Ezviz (`ErrCode/5405` e `6519`) sia i tag `#EXT-X-ENDLIST` di fine playlist fittizi inviati dal cloud durante il risveglio dello spioncino, attendendo fino a 14s l'arrivo dei primi frammenti `.ts` reali prima di reindirizzare il player.
- **Tuning Player HLS.js**: Configurate finestre di timeout estese (15s/20s) e fino a 10 retry automatici su frammenti e manifest per consentire al player HTML5 autonomo di tollerare il risveglio senza blocchi o schermate nere.

---

## [1.7.5] — 2026-10-12

### Fixed & Improved
- **Live Stream HLS Wake-up Verification**: Risolto l'errore di Home Assistant (*"Timestamp discontinuity detected"* / schermata bianca) quando si avvia lo streaming live con telecamere a batteria (HP2). L'endpoint `/api/live/stream.m3u8` ora include un meccanismo di pre-verifica e wake-up che attende il risveglio completo della telecamera, evitando che HA riceva i segmenti fittizi di errore Ezviz (`ErrCode/6519`) e garantendo una riproduzione fluida dei veri frame H.264.
- **Persistenza Cache AccessToken Open Platform**: Risolto il problema per cui a ogni riavvio o aggiornamento dell'Add-on veniva generata una nuova coppia di credenziali/token su Ezviz. Il token ora viene salvato nello storage persistente `/data/open_token.json` e riutilizzato finché non scade (validità 7 giorni).
- **Risolto Timeout Player Standalone**: Adattato il caricamento del player video HTML5 per risolvere i percorsi relativi sia quando viene aperto da Ingress sia quando viene aperto come pagina web autonoma (`/player?live=true`), con gestione automatica della riconnessione e dei retry su `hls.js`.

---

## [1.7.4] — 2026-10-12

### Fixed & Improved
- **URL Integrazione Telecamera Corretti**: Aggiornati i suggerimenti nella Dashboard e nella documentazione con l'hostname di rete Docker reale (`http://a70a6fb7-ezviz-camera-proxy:8099/...`) al posto del fuorviante `localhost:8099` (che provocava 404 all'interno dei container HA).
- **Aggiunto URL Stream Live HLS Nativo**: Aggiunto l'indirizzo `/api/live/stream.m3u8` nei suggerimenti per la configurazione come *Generic Camera* in alta definizione su Home Assistant.
- **Copy-to-Clipboard con un Click**: Aggiunta la funzione di copia istantanea negli appunti con toast di conferma cliccando su ciascun URL.

---

## [1.7.3] — 2026-10-12

### Fixed & Improved
- **Download Clip MP4 On-Demand & Feedback Interattivo**: Risolto il problema del download di file da 58 byte (che corrispondeva al messaggio JSON di clip non ancora elaborata). Il pulsante "MP4" ora gestisce il download in modo intelligente: se il file non è ancora salvato in locale, attiva la generazione on-demand via FFmpeg con spinner e notifica di avanzamento, scaricando il vero file MP4 senza errori.
- **Parametri FFmpeg Ottimizzati**: Aggiunti socket timeout (`-timeout 15000000`) e `-movflags +faststart` per prevenire blocchi o corruzioni durante il fetch dei segmenti HLS da Ezviz Cloud.
- **Nuovo Riproduttore Cronologia (Slideshow)**: Sostituito il fragile stream MJPEG del browser con uno slideshow client-side fluido e reattivo che scorre le istantanee salvate in ordine cronologico con contatore dei frame (`1/N`) e passaggio immediato tra Live e Cronologia.

---

## [1.7.2] — 2026-10-12

### Fixed
- **MQTT Event Attributes**: Risolto bug di inizializzazione su `attr_data` durante gli eventi real-time push che preveniva la pubblicazione corretta degli attributi verso Home Assistant.

---

## [1.7.1] — 2026-10-12

### Added
- **Download Automatico Clip MP4 con FFmpeg**: Aggiunto `ffmpeg` nei binari dell'Add-on (`apk add ffmpeg`) e implementato un worker in background che scarica e archivia automaticamente i filmati degli eventi registrati su SD in formato nativo `.mp4` ad alta definizione (`-c copy`, consumo CPU irrisorio).
- **Nuovo Endpoint Clip Video**: Esposto `/api/events/clip/<alarm_id>` per scaricare o riprodurre direttamente il file `.mp4` locale senza dipendere dalla sessione di streaming cloud.
- **Pulsante di Download MP4 nella Dashboard**: Aggiunto il pulsante "MP4" in ciascuna riga dell'elenco eventi per scaricare il file video sul computer o smartphone con un click.
- **Attributo MQTT `clip_url`**: I messaggi MQTT di notifica includono ora anche `clip_url` per allegare direttamente il file `.mp4` locale nelle notifiche o inviarlo via bot Telegram/webhook.

---

## [1.7.0] — 2026-10-12

### Added
- **Ezviz Open Platform & Native HLS Video Streaming**: Integrazione completa con la piattaforma ufficiale Ezviz Open (`ieuopen.ezvizlife.com` per l'Europa) tramite `appKey`, `appSecret` e `accessToken`.
- **Riproduzione Video Nativi degli Eventi**: Endpoint `/api/events/video/<alarm_id>` che calcola il lasso temporale dell'evento registrato su scheda MicroSD locale e reindirizza al flusso nativo HLS (`.m3u8`) ad alta definizione, azzerando le perdite di fotogrammi rispetto al campionamento a scatti.
- **Live Stream HLS Nativo**: Endpoint `/api/live/stream.m3u8` per visualizzare in tempo reale il video HD nativo H.264 direttamente su Home Assistant e browser.
- **Web Player HTML5 Universale**: Interfaccia player `/player?alarm_id=...` basata su `hls.js` con supporto nativo per iOS/Safari, integrata nella dashboard con un pratico modal e comandi dedicati "Video" e "Live Nativo HLS".
- **Attributi Evento MQTT Arricchiti**: I payload degli eventi MQTT pubblicati verso Home Assistant ora includono automaticamente `video_url` e `player_url` per notifiche rich push con video e azioni rapide.

---

## [1.6.7] — 2026-05-11

### Fixed
- **MQTT Discovery**: Rimosso il `device_class` "doorbell" non valido per `binary_sensor` nella configurazione MQTT Auto-Discovery, prevenendo errori di validazione nel log di Home Assistant.
- **Error Handling API**: Aggiunto un meccanismo di fallback sicuro in `ezviz_client.py` che restituisce i dati della cache quando il recupero della pagelist fallisce a causa di un timeout di rete (es. "Connection reset by peer" o "Read timed out"), evitando che l'applicazione riporti erroneamente "Device non trovato". Migliorato anche il messaggio d'errore.

---

## [1.6.6] — 2026-05-09

### Changed
- **Rebranding & Ownership**: Presa in carico formale dell'ownership dell'intero progetto da parte di Francesco D'Offizi (`francescodoffizi/ha-ezviz-stream`), con riscrittura e aggiornamento di tutti i metadati e URL del repository.
- **Localizzazione**: Traduzione integrale e pulizia in lingua italiana di tutta la documentazione principale, inclusi `README.md`, `DOCS.md` e i campi di descrizione dell'Add-on.

---

## [1.6.5] — 2026-05-09

### Fixed
- **Thread Loop**: Rimosso l'avvio duplicato globale dei thread in background (`_snapshot_worker` e `_local_mqtt_worker`). I thread vengono ora avviati solo all'interno del blocco `__main__`, risolvendo il problema delle continue disconnessioni/riconnessioni MQTT (collisione Client ID) e dei login doppi alle API di Ezviz.

---

## [1.6.4] — 2026-05-09

### Fixed
- **Home Assistant Limits**: Intercettati e accorciati gli URL delle immagini Cloud Ezviz (> 255 caratteri) nei payload MQTT e nelle risposte REST API (`/api/status`, `/api/events`). Gli URL puntano ora al proxy locale (es: `/api/events/image/<id>`), evitando crash di Home Assistant dovuti al limite stringente di 255 caratteri per lo stato dei sensori.

---

## [1.6.2] — 2026-04-25

### Fixed
- **Deduplicazione**: Migliorata logica di unione eventi fuzzy (10s) per evitare doppioni tra Push e Polling cloud.
- **Stato**: Ripristinata la visualizzazione di "Ultima suonata" e "Ultimo evento" nella sidebar.
- **Playback**: Aggiunto tasto "Riproduci Loop" nella modale evento per visione MJPEG ricostruita.

## [1.6.1] — 2026-04-25

### Fixed
- **MJPEG Stream**: Rifatto il generatore MJPEG per una migliore compatibilità con i browser e HA Ingress.
- **Playback Storia**: Corretto il loop di riproduzione della cronologia eventi.
- **Dashboard**: Risolto il bug che non aggiornava il timestamp dell'ultimo update nella dashboard.

## [1.6.0] — 2026-04-25

### Added
- **Localizzazione**: Localizzazione completa in italiano (Dashboard, entità MQTT e messaggi di stato).

### Fixed
- **Frontend**: Migliorata la gestione degli errori e la visualizzazione delle date.
- **MQTT**: Nomi delle entità tradotti in italiano per una migliore integrazione con Home Assistant.

## [1.5.0] — 2026-04-25

### Fixed
- **Stability**: Refactored MJPEG stream generation to use standard boundary formatting for better browser compatibility.
- **Frontend**: Added robust error catching and logging to all API polls.
- **Frontend**: Fixed "Invalid Date" display for last snapshot time.
- **Authentication**: Improved modal logic to ensure consistent token handling during history playback.

## [1.4.9] — 2026-04-25

### Fixed
- **History Playback**: Resolved rendering issues (black screen) by switching to a more standard MJPEG format with unique boundaries and explicit `Content-Length` headers.
- **MJPEG Stability**: Removed redundant cache-busting parameters from MJPEG stream URLs which could interfere with long-lived MJPEG connections.
- **Stream Termination**: Improved client-side stream termination when closing modals to save bandwidth and server resources.

---

## [1.4.8] — 2026-04-25

### Fixed
- **401 Error on MJPEG**: Replaced external "Open MJPEG Stream" link with an in-page modal to maintain Home Assistant Ingress authentication.
- **Play History**: Improved reliability by forcing a re-render of the stream element when toggling history mode.
- **Backend Logging**: Added monitoring to `get_image_list` to track historical image availability on disk.

---

## [1.4.7] — 2026-04-25

### Fixed
- **Play History**: Fixed a dashboard bug where the snapshot countdown was not paused during history playback, causing the browser to overwrite the MJPEG stream with a static snapshot URL.
- **MJPEG Compatibility**: Optimized the stream generator to omit `Content-Length` headers and improved frame boundary formatting for better compatibility with different browsers.
- **Enhanced Logging**: Added more detailed logs for image retrieval and stream generation to aid troubleshooting.

---

## [1.4.6] — 2026-04-25

### Changed
- **Dashboard Modal**: Replaced event thumbnail links with an in-page modal/lightbox. This prevents 401 errors when opening images, as it keeps the user within the authenticated Home Assistant Ingress session.
- **Aggressive Deduplication**: Refined `EventStore` to merge generic event codes (like `10120`) with specific detection events (Face/Human) when they occur at the same timestamp.

### Security
- **Cloud Privacy**: Confirmed that the proxy does not access any cloud video recording or live streaming APIs. It only uses thumbnails and status data.

---

## [1.4.5] — 2026-04-25

### Fixed
- **Image Decryption**: Implemented automatic decryption for event thumbnails using the user-provided `ezviz_encryption_key`.
- **Event Field Mappings**: Fixed `timeStr`, `pic`, and `title` extraction from the unified message API to ensure accurate event data.
- **Background Workers**: Fixed a critical bug where snapshot polling and MQTT listener threads were not being started on boot.

### Added
- **Encryption Configuration**: Added `ezviz_encryption_key` option to the add-on configuration.

---

## [1.4.4] — 2026-04-25

### Added

- **HA Refresh Button**: Exposed the snapshot refresh function to Home Assistant as an MQTT button. This allows triggering a cloud snapshot fetch via automations or the HA UI using the new `button.ezviz_<serial>_refresh_snapshot` entity.
- **Local MQTT Command Listener**: Implemented a background listener for local MQTT commands to handle the refresh trigger with low latency.

---

## [1.4.3] — 2026-04-23

### Fixed

- **Event Flood Prevention**: Refined `EventStore` logic to protect rich push-notification data (exact timestamps and specific event names) from being overwritten by generic polling data.
- **Improved Time Accuracy**: Enhanced `EzvizClient` to extract timestamps from raw integer fields when cloud string representations are missing.
- **Fuzzy Deduplication**: Implemented a 1-second fuzzy match for event timestamps to handle drift between real-time push and cloud polling.

---

## [1.4.2] — 2026-04-22

### Fixed

- **Event Merging**: Fixed a deduplication bug where real-time push events (numeric codes) and cloud polling (string names) would duplicate. Normalization map now ensures consistent event names.
- **Push Photo Recovery**: Implemented a deferred cloud polling refresh (5s after push) to ensure event thumbnails are fetched even when missing from the raw push payload.
- **Dashboard Refresh**: Fixed "Loading status..." stuck state in the UI by refactoring the status poller to dynamically rebuild the attribute list.

---

## [1.4.1] — 2026-04-22

### Fixed

- **Event Deduplication**: Improved logic in `EventStore` to merge events based on a composite key (time + type). This fixes duplication when cloud push and polling return different ID formats for the same physical event.
- **Push Message Accuracy**: Refined `_on_ezviz_push_message` to extract exact event timestamps from the payload, preventing incorrect dates in the dashboard.
- **MJPEG Stream Stability**: Fixed a bug where the historical MJPEG stream would stop if an image was missing. It now skips missing frames and loops correctly.

### Added

- **UI Thumbnails**: Added clickable event thumbnails directly to the "Recent Events" list on the dashboard for immediate visibility.

---

## [1.4.0] — 2026-04-22

### Added

- **Enhanced Event UI**: Replaced "View image" links with dedicated buttons and eye icons for better visibility and usability.

---

## [1.3.9] — 2026-04-22

### Fixed

- **Event Duplication**: Fixed a bug in `EventStore` where ID type mismatch (int vs str) caused duplicated entries.
- **Improved Push Deduplication**: Enhanced fallback ID generation for push messages when unique IDs are missing from the cloud.

---

## [1.3.8] — 2026-04-22

### Fixed

- **Event Reliability**: Implemented `EventStore` for proper merging and deduplication of real-time push events and polled cloud events.
- **Missing Event Data**: Normalized event fields (`alarm_pic_url`, `alarm_time`) to ensure consistency between different event sources.
- **UI Bugs**: Fixed Javascript errors in the event list and image loading.

### Added

- **Local Snapshot Storage**: Added automatic local caching of event images to ensure they remain available after cloud URLs expire.
- **Snapshot History Playback**: Added a new "Play History" feature to the dashboard and MJPEG stream to loop through recent event snapshots.

---

## [1.3.7] — 2026-04-13

### Added

- **Battery Optimization**: Increased background status polling interval to 300s (5 minutes) when `SNAPSHOT_INTERVAL` is 0, relying on real-time push for events.
- **Deep Debug**: Enhanced `/api/debug/alarms` to search multiple message subtypes (`101`, `102`, `2701`) for doorbell events.

---

## [1.3.6] — 2026-04-12

### Added

- **Debug endpoint**: Added `/api/debug/alarms` to allow manual verification of cloud event history. This helps in diagnosing why push events might be missing (e.g. cloud misconfiguration).

---

## [1.3.5] — 2026-04-12

### Fixed

- **Event Processing**: Fixed a regression in the real-time event handler and improved event ID extraction to ensure all push messages are processed correctly.

---

## [1.3.4] — 2026-04-12

### Fixed

- **Cleaner Entity Names**: Simplified sensor names in MQTT Discovery to avoid redundant serial numbers. This should result in cleaner entity IDs in Home Assistant like `binary_sensor.ezviz_camera_bh9350432_motion`.

---

## [1.3.3] — 2026-04-12

### Fixed

- **Flattened MQTT Attributes**: Event data in binary sensors is now flattened, allowing direct access to the `image` URL via `state_attr(..., 'image')` in Home Assistant automations.

---

## [1.3.2] — 2026-04-12

### Added

- **Global Alarm Sensor**: Added `binary_sensor.ezviz_{serial}_alarm` that triggers for ANY detected event (motion, doorbell, person, etc.). This ensures no event is missed while debugging specific codes.
- **Enhanced Debug Logging**: The raw payload of every push event is now logged to help identify unknown doorbell codes.

### Fixed

- **Expanded Doorbell Detection**: Added more potential doorbell event codes (`10000`, `10054`, `10055`, etc.) to the detection logic.

---

## [1.3.1] — 2026-04-12

### Added

- **MQTT Attributes**: Binary sensors for Motion and Doorbell now include the full Ezviz JSON event data as attributes. This makes it easy to extract the `image` URL for Home Assistant notifications.

---

## [1.3.0] — 2026-04-12

### Fixed

- **Ezviz Cloud MQTT Stability**: Fixed a scoping bug that caused the connection to the Ezviz event stream to be unstable and reset every minute.
- **Local MQTT Discovery**: Verified and confirmed that auto-discovery messages are correctly sent to Home Assistant.

---

## [1.2.9] — 2026-04-12

### Added

- **Manual MQTT Configuration**: Added options to manually specify MQTT host, port, username, and password in the add-on configuration. This serves as a definitive fallback if auto-detection fails.

### Fixed

- **Bashio Service Detection**: Switched to a more robust `bashio::service` syntax to fix "command not found" errors during startup.

---

## [1.2.8] — 2026-04-12

### Fixed

- **Boot Crash**: Fixed "unbound variable" error in `run.sh` that caused the add-on to crash during startup.
- **Bashio Compatibility**: Updated MQTT service detection to use `bashio::services.available` for better compatibility with current Home Assistant versions.

---

## [1.2.7] — 2026-04-12

### Fixed

- **Event List recovery**: Fixed the issue where the events table on the dashboard was empty when the snapshot interval was set to 0. Events are now polled every 60 seconds from the cloud, and real-time push events are instantly added to the list.

---

## [1.2.6] — 2026-04-12

### Added

- **MQTT Diagnostics**: Added verbose startup logs to identify why Home Assistant MQTT services are not being detected automatically.
- **MQTT Fallback**: Added a fallback detection for `core-mosquitto` broker in common Home Assistant setups.

---

## [1.2.5] — 2026-04-12

### Added

- **Home Assistant MQTT Discovery**: Automatically registers "Motion" and "Doorbell" sensors in Home Assistant. They will now appear as devices/entities without manual YAML configuration.
- **Specific Event Topics**: Now publishing to `homeassistant/camera/ezviz/{serial}/doorbell` and `homeassistant/camera/ezviz/{serial}/motion` as requested.

### Fixed

- **Event Mapping**: Improved logic to distinguish between person/motion events and doorbell presses based on Ezviz alert codes.

---

## [1.2.4] — 2026-04-12

### Added

- **Auto-Refresh Dashboard**: The UI now automatically refreshes the snapshot and battery level whenever a new event is detected or a manual refresh occurs.
- **MQTT Auto-Discovery**: Fixed MQTT service detection in Home Assistant Supervisor; the add-on now correctly identifies the local broker for alarm publishing.

### Fixed

- **Event Reliability**: Moved event-driven snapshot triggering to run even if MQTT is not configured locally.

---

## [1.2.3] — 2026-04-12

### Added

- **Enhanced Event Logging**: Added verbose logging for Ezviz Cloud push messages to debug doorbell and motion detection issues in real-time.

---

## [1.2.2] — 2026-04-12

### Fixed

- **Aggressive Auth Backoff**: Increased the authentication retry cooling period to 60 seconds globally and to 5 minutes for the background worker. This is specifically designed to help recover from Ezviz Error 1069 ("terminal limit reached") by giving the account time to clear stale sessions.

---

## [1.2.1] — 2026-04-12

### Fixed

- **UI Refresh Loop**: Fixed a bug where setting `snapshot_interval` to `0` caused the dashboard to refresh every second.
- **Auth Flood Protection**: Added a 30-second "cooling period" after any authentication failure. This prevents the proxy from hammering Ezviz servers and hitting the "too many terminals" (error 1069) limit when sessions expire or credentials issues occur.

---

## [1.2.0] — 2026-04-12

### Added

- **Event-Driven Snapshots (Battery Saver)**: The add-on now allows `snapshot_interval` to be set to `0` (or any large number up to completely disabled). When a real-time push event (motion or doorbell) arrives via Ezviz MQTT, an immediate, lightweight threaded snapshot fetch is forced instantly, perfectly synchronized with the event. This drastically saves battery on the HP2 camera, as the proxy no longer forcefully wakes the device up on an arbitrary polling clock.

---

## [1.1.4] — 2026-04-12

### Added

- **Real-Time Push Notifications**: Migrated the internal architecture from 60-second API polling to zero-latency MQTT push. The proxy now maintains a persistent connection to Ezviz Cloud's push servers. When motion or a doorbell ring occurs, the event is immediately intercepted and forwarded to the local Home Assistant MQTT broker, eliminating the polling delay entirely!

---

## [1.1.3] — 2026-04-12
- Support Content-Length in MJPEG streams

---

## [1.1.2] — 2026-04-12

### Fixed

- **MJPEG Stream Timeout**: Decreased the MJPEG `frame_delay` in `/api/stream` to 1.0 seconds (1 FPS constant). Previously the delay was bound to the `SNAPSHOT_INTERVAL` (e.g. 30 seconds), causing strict clients like Home Assistant Generic Camera to drop the connection due to timeout.

---

## [1.1.1] — 2026-04-12

### Fixed

- **API Ports:** Exposed container port `8099` to the Home Assistant host network. Previously, endpoints like `/api/snapshot` and `/api/stream` were only accessible internally via HA Ingress and not exposed externally for integrations like Generic Camera.

---

## [1.1.0] — 2026-04-12

### Added

- **MQTT Event Forwarding**: Fully implemented MQTT logic in `server.py` to publish new events from Ezviz Cloud directly to the local Home Assistant MQTT Broker (on topic `ezviz/<CAMERA_SERIAL>/alarm`).
- Required the `mqtt: want` service in the add-on configuration, allowing Home Assistant to securely feed the broker connection parameters to the add-on dynamically.

### Changed

- Updated core dependency `pyezvizapi` to version `>=1.0.4.5` for better stability and latest endpoint capabilities.
- Added a smart retry and wait mechanism inside the event fetcher. If a newly discovered alarm on the cloud lacks an image (which happens frequently due to battery camera upload delays), the proxy will wait a few seconds and try fetching it again to improve the image success rate on Home Assistant templates.

---

## [1.0.1] — 2026-04-02

### Fixed

- **Critical:** Bypass `pyezvizapi`'s `get_device_infos()` which crashes with
  `'str' object has no attribute 'get'` on HP2 cameras. The HP2 returns string
  values instead of dicts in the CLOUD section of the pagelist response.
  New implementation calls `_get_page_list()` directly and safely parses all
  sections, fully bypassing the broken code path.
- Fixed `datetime.utcnow()` deprecation warnings (replaced with
  `datetime.now(timezone.utc)`).
- Improved snapshot worker with consecutive error tracking and backoff.
- Default `snapshot_interval` increased from 30s to 60s (HP2 is battery-powered).

---

## [1.0.0] — 2026-04-02

### Added

- Initial release of the Ezviz Camera Proxy add-on
- Ezviz Cloud API integration via `pyezvizapi`
- Periodic snapshot polling with configurable interval (5–300 seconds)
- Token caching to `/data/ezviz_token.json` for reduced login calls
- HTTP endpoints:
  - `GET /api/snapshot` — Latest cached snapshot as JPEG
  - `POST /api/snapshot/refresh` — On-demand cloud snapshot fetch
  - `GET /api/status` — Camera status (online, battery, WiFi signal, firmware)
  - `GET /api/events` — Recent alarm events list
  - `GET /api/stream` — Simulated MJPEG stream from cached snapshots
  - `GET /api/devices` — All devices on the Ezviz account
  - `GET /api/health` — Add-on health check
- Home Assistant Ingress support with sidebar panel (`mdi:doorbell-video`)
- Built-in dark-themed Web UI dashboard:
  - Real-time snapshot display with auto-refresh
  - Camera status panel (battery level, WiFi, online/offline)
  - Recent events/alarms list
  - Manual refresh button
  - MJPEG stream link
  - HA Generic Camera URL helper
- MQTT event publishing for doorbell and motion detection (optional)
- Auto-reconnect on session expiry
- Placeholder JPEG image for "no snapshot yet" state
- Support for architectures: `amd64`, `aarch64`, `armv7`, `armhf`, `i386`
- English translations for all config options
- Comprehensive documentation (DOCS.md)

### Notes

- The Ezviz HP2 is battery-powered and enters deep sleep between events.
  Each snapshot fetch briefly wakes the device. Adjust `snapshot_interval`
  to balance responsiveness and battery life.
- RTSP and LAN Live View are not supported by the HP2 hardware/firmware.
  This add-on uses the Ezviz Cloud API as the only available access path.
