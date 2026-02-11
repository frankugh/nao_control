# NAOqi 2.1 (NAO v5) Reference Links

Doel: vaste plek met primaire documentatie voor perceptie/awareness modules die we in dit project gebruiken.

## Core docs

- NAOqi People Perception overview  
  https://fileadmin.cs.lth.se/robot/nao/doc/naoqi/peopleperception/index.html
- ALBasicAwareness  
  https://fileadmin.cs.lth.se/robot/nao/doc/naoqi/peopleperception/albasicawareness.html
- ALGazeAnalysis  
  https://fileadmin.cs.lth.se/robot/nao/doc/naoqi/peopleperception/algazeanalysis.html
- ALGazeAnalysis API  
  https://fileadmin.cs.lth.se/robot/nao/doc/naoqi/peopleperception/algazeanalysis-api.html
- ALPeoplePerception  
  https://fileadmin.cs.lth.se/robot/nao/doc/naoqi/peopleperception/alpeopleperception.html
- ALFaceDetection  
  https://fileadmin.cs.lth.se/robot/nao/doc/naoqi/peopleperception/alfacedetection.html
- ALTracker API  
  https://fileadmin.cs.lth.se/robot/nao/doc/naoqi/trackers/altracker-api.html

## Important practical note

NAOqi docs describe API capabilities for the 2.1 family. A concrete robot image can still miss specific services/modules.
Always verify availability at runtime.

## Runtime checks (via existing endpoint)

Examples against local Py2 base controller:

```bash
curl -X POST http://localhost:5000/naoqi/call -H "Content-Type: application/json" -d "{\"module\":\"ALGazeAnalysis\",\"method\":\"getTolerance\"}"
curl -X POST http://localhost:5000/naoqi/call -H "Content-Type: application/json" -d "{\"module\":\"ALPeoplePerception\",\"method\":\"getMaximumDetectionRange\"}"
curl -X POST http://localhost:5000/naoqi/call -H "Content-Type: application/json" -d "{\"module\":\"ALBasicAwareness\",\"method\":\"isAwarenessRunning\"}"
curl -X POST http://localhost:5000/naoqi/call -H "Content-Type: application/json" -d "{\"module\":\"ALFaceDetection\",\"method\":\"subscribe\",\"args\":[\"diag\",500,0.0]}"
```

