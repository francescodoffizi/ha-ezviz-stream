# Ezviz Camera Proxy — Add-on per Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/francescodoffizi/ha-ezviz-stream)](https://github.com/francescodoffizi/ha-ezviz-stream/releases)
[![License](https://img.shields.io/github/license/francescodoffizi/ha-ezviz-stream)](LICENSE)

Un Add-on per Home Assistant che integra i spioncini e le telecamere **Ezviz HP2** (e altre telecamere Ezviz senza supporto RTSP) nella tua domotica utilizzando l'API Ezviz Cloud.

---

## Perché questo Add-on?

Lo spioncino smart **Ezviz CS-HP2**:

- ✅ Funziona benissimo con l'applicazione ufficiale Ezviz
- ✅ Supporta il rilevamento di movimento, eventi campanello e video 1080p
- ❌ **Non** supporta il protocollo RTSP
- ❌ **Non** supporta la visualizzazione live in rete locale (LAN)
- ❌ Non può essere integrato con le normali piattaforme telecamera di Home Assistant

Questo add-on risolve il problema fungendo da proxy locale tra Home Assistant e l'API Ezviz Cloud.

---

## Caratteristiche

- 📸 **Snapshot polling** — Istantanee periodiche dal cloud salvate in locale e servite in formato JPEG.
- 📹 **MJPEG stream** — Simulazione di un flusso video live basato sulle istantanee salvate (compatibile con la piattaforma Generic Camera di HA).
- 🔋 **Batteria e stato** — Stato online, livello della batteria e potenza del segnale Wi-Fi esposti tramite REST API.
- 🔔 **Eventi Allarme** — Notifiche in tempo reale di movimento e campanello, con supporto per l'invio su MQTT.
- 🌐 **Web UI** — Dashboard integrata accessibile direttamente da Home Assistant Ingress (pannello laterale).
- 🔒 **Sicurezza** — Le credenziali vengono salvate nella configurazione protetta dell'add-on di HA e mai registrate nei log.

---

## Installazione

1. Aggiungi questo repository al tuo Add-on Store di Home Assistant:
   ```
   https://github.com/francescodoffizi/ha-ezviz-stream
   ```
2. Installa **Ezviz Camera Proxy**
3. Configura l'add-on con le tue credenziali Ezviz e il numero seriale della fotocamera
4. Avvia l'add-on

---

## Configurazione Rapida

```yaml
ezviz_username: "latua@email.com"
ezviz_password: "la-tua-password"
ezviz_region: "apiieu.ezvizlife.com"
camera_serial: "AB1234567"
camera_password: "codice-verifica-camera"
snapshot_interval: 30
enable_mqtt_events: true
```

---

## Aggiungere come Entità Telecamera in HA

```yaml
camera:
  - platform: generic
    name: "Spioncino HP2"
    still_image_url: "http://localhost:8099/api/snapshot"
    stream_source: "http://localhost:8099/api/stream"
```

---

## Documentazione

Consulta il file [DOCS.md](DOCS.md) per i dettagli sulla configurazione, esempi di integrazione con HA, documentazione delle API e risoluzione dei problemi.

---

## Architetture Supportate

`amd64` · `aarch64` · `armv7` · `armhf` · `i386`

---

## Licenza

MIT — vedi [LICENSE](../../LICENSE)
