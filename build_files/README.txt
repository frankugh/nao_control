===============================
 NAO Controller - Portable Build
===============================

Deze map bevat de volledige portable runtime voor de NAO-controller:
- Py2 NAO Base Controller (communicatie met NAO via SDK)
- Py3 Behavior Manager (Piper-TTS, NAO-actions, script-runner)
- Eén centrale config.ini

-----------------------------------
 1. Starten van de controllers
-----------------------------------

Dubbelklik:

    start_all.bat

Dit opent twee vensters:
- "NAO Py2": NAO basiscontroller (wake-up, rest, behaviors, TTS native)
- "NAO Py3": Behavior Manager + Piper-TTS + NAO-proxy API

Beide processen blijven draaien tot je het venster sluit.

-----------------------------------
 2. Configuratie
-----------------------------------

Alle instellingen staan in:

    config.ini  (in deze map)

Voorbeelden van aanpasbare velden:
- WEB_HOST, WEB_PORT
- NAO_IP, NAO_PORT
- NAO_SSH_USER, NAO_SSH_PASS
- NAO_REMOTE_AUDIO_DIR
- BEHAVIOR_MANAGER_PORT (Py3)

Na het aanpassen van config.ini: 
→ gewoon opnieuw opstarten via start_all.bat.

-----------------------------------
 3. Bestandstructuur runtime
-----------------------------------

 build\
    config.ini
    start_all.bat
    readme.txt
    nao_base_controller\
        nao_base_controller.exe
        naoqi-sdk\
            lib\
            bin\
    behavior_manager\
        nao_behavior_manager.exe

-----------------------------------
 4. Voorwaarden
-----------------------------------

- Windows machine
- Geen Python-installatie vereist
- Geen NAO SDK-installatie vereist
- Toegang tot het NAO IP-adres via netwerk

-----------------------------------
 5. Testen
-----------------------------------

curl -X POST http://localhost:5001/nao/naoqi/call \
  -H "Content-Type: application/json" \
  -d '{
    "module": "ALTextToSpeech",
    "method": "say",
    "args": ["Hallo via de generieke NAOqi-call"],
    "kwargs": {}
  }'

-----------------------------------
 6. Herbuilden (development)
-----------------------------------

Gebruik in de projectroot:

    build.bat

Dit pakt automatisch:
- De py2- en py3-servers uit source
- De geprune’de SDK
- De bron config.ini
- De readme.txt & start_all.bat uit build_files\

-----------------------------------
Einde
-----------------------------------
