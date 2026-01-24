# NAO Command Recognition Training (NL)

Dit project bevat de training/export pipeline voor een minimal maar productie-achtig command recognition model (NL) voor NAO. Runtime integratie gebeurt later in de dialog manager.

## Vereisten

- Python 3.10+
- Installatie:

```bash
pip install -e .
```

## 1) Commands dataset vullen

Bewerk `data/commands_raw.md` met de huidige dataset in edit-friendly markdown (bullets per label). Labels worden herkend via `## LABEL` koppen. Alle bullets onder `## DANCE` blijven `DANCE`. Locomotion labels worden bij export gerelabeld naar `LOCOMOTION_REQUEST`.
Hard negatives/meta-praat staan in `data/none_candidates.txt` (1 zin per regel). Seed-zinnen voor NONE staan in `data/none_seed.txt`.

## 2) Commands exporteren

```bash
python tools/export_commands.py
```

Output: `data/commands.jsonl`

## 3) NONE/OTHER genereren

```bash
python tools/make_none.py --seed-file data/none_seed.txt --download-hf --max-none 7000
```

`--download-hf` vereist internet en de optional dependency `datasets`. Je kunt `--max-none 7000` gebruiken om het aantal NONE samples te begrenzen.

Als je hard negatives in `data/none_candidates.txt` wilt meenemen:

```bash
python tools/export_none_candidates.py
python tools/make_none.py --seed-file data/none_seed.txt --download-hf --max-none 7000
```

### NONE split outputs (optioneel)

Je kunt `make_none.py` ook laten splitsen in seed vs. external:

```bash
python tools/make_none.py \
  --seed-file data/none_seed.txt \
  --download-hf \
  --max-none 7000 \
  --output data/none.jsonl \
  --output-seed data/none_seed.jsonl \
  --output-external data/none_external.jsonl
```

## 4) Review (optioneel)

```bash
python tools/sample_review.py
python tools/apply_overrides.py
```

### Active learning review queue

Gebruik deze scripts om de AL review queue te exporteren en toe te passen:

```bash
python tools/export_review_queue.py --queue data/al/review_queue.jsonl
# edit review/al_review.csv
python tools/apply_review_queue.py --review review/al_review.csv
```

De export sorteert op: declined eerst, daarna laagste confidence.
Als `keep` en `reviewed_label` leeg zijn wordt de rij genegeerd.
Gebruik:
- `keep=1` als het suggested label klopt
- `keep=0` om de rij te droppen
- `reviewed_label` om het label te corrigeren
Na apply worden alle reviewed IDs automatisch uit de queue verwijderd.

Optioneel kun je auto-retrainen zodra er genoeg reviews zijn:

```bash
python tools/apply_review_queue.py --review review/al_review.csv --auto-retrain
```

Auto-retrain schrijft eerst naar `dist/experiments/` en promoot alleen naar `dist/` als de score beter is.
Auto-retrain werkt cumulatief: reviews tellen op tot de threshold is bereikt.
Standaard pakt auto-retrain de 25 minst zekere auto-approved samples met confidence <= 0.75.

## 5) Trainen

```bash
python -m cmdrec.train --commands data/commands.jsonl --none data/none.jsonl --out dist/bundle_vX
```

Training gebruikt `data/none_clean.jsonl` als die bestaat en kapt NONE tot maximaal 5x het aantal commands.

### Retrain (Gold v1 + ratio)

Eerst eenmalig een vaste gold-split maken:

```bash
python tools/make_gold_split.py --seed 42 --gold-ratio 0.2
```

Daarna retrainen met vaste gold-set en gecontroleerde NONE-ratio:

```bash
python tools/retrain_cmdrec.py --none-ratio 5 --out dist/bundle_v17
```

Auto-out (naam op basis van laatste bundle) kan met:

```bash
python tools/retrain_cmdrec.py --out auto
```

## 6) Voorspellen

```bash
python -m cmdrec.predict "boks"
```

Bij `DANCE` wordt ook de dance-key resolved via `data/dance_catalog.json` (of de bundle copy).

## Runtime notes

- STOP is rule-first: eerst `stop_rules(text)` en daarna pas ML fallback.
- Locomotion control mode is runtime-only; in dit repo blijft alleen het ML label `LOCOMOTION_REQUEST` + resolver.

## Bundle output

`dist/bundle_v1/` bevat:

- `model.joblib` (sklearn Pipeline)
- `labels.json` (label lijst)
- `decision_policy.json` (thresholds, margin, stop rules, constraints)
- `train_report.json` (metrics + gekozen configuratie)
- `dance_catalog.json` (resolver catalog voor DANCE)
