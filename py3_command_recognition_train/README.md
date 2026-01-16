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

## 4) Review (optioneel)

```bash
python tools/sample_review.py
python tools/apply_overrides.py
```

## 5) Trainen

```bash
python -m cmdrec.train --commands data/commands.jsonl --none data/none.jsonl --out dist/bundle_vX
```

Training gebruikt `data/none_clean.jsonl` als die bestaat en kapt NONE tot maximaal 5x het aantal commands.

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
