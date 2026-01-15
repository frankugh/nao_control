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

## 2) Commands exporteren

```bash
python tools/export_commands.py
```

Output: `data/commands.jsonl`

## 3) NONE/OTHER genereren

### Van eigen file

```bash
python tools/make_none.py --from-file data/commands_raw.md
```

### (Optioneel) via HuggingFace

```bash
python tools/make_none.py --download-hf
```

Let op: `--download-hf` vereist internet en de optional dependency `datasets`.

## 4) Trainen

```bash
python -m cmdrec.train --commands data/commands.jsonl --none data/none.jsonl --out dist/bundle_v1
```

## 5) Voorspellen

```bash
python -m cmdrec.predict "boks"
```

## Bundle output

`dist/bundle_v1/` bevat:

- `model.joblib` (sklearn Pipeline)
- `labels.json` (label lijst)
- `decision_policy.json` (thresholds, margin, stop rules, constraints)
- `train_report.json` (metrics + gekozen configuratie)
