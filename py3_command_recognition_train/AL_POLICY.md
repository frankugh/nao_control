# Active Learning Policy (AL Policy)

Scope: Command recognition (cmdrec) model training data, validation strategy, and review workflow.

## Goals
- Keep model quality measurable over time.
- Prefer clean, user-derived data for training.
- Make review and retrain workflows lightweight for a hobby project.

## Definitions
- **Gold v1**: fixed validation set (initially `commands_raw.md` + `none_seed.txt`).
- **Gold v2**: future replacement for Gold v1 with higher-quality NONE data.
- **Auto-train**: examples accepted without manual review.
- **Review queue**: examples that must be reviewed before training.

## Data Sources
1) **Approved command** (guarded command confirmed):
   - Label = command.
   - Goes to **auto-train**.

2) **Declined command**:
   - Goes to **review queue**.
   - Must be reviewed before training.

3) **Auto-executed command** (unguarded):
   - Treated like **NONE** for review purposes.
   - Goes to **review queue**.

4) **No command recognized (NONE)**:
   - Goes to **review queue**.
   - Must be reviewed before training.

5) **External NONE data (online dialog dataset)**:
   - Stored separately as supplemental data.
   - Used only to balance class ratios (NONE = 7x command), not as Gold.

## Validation Strategy
- **Primary validation**: always evaluate on **Gold v1** for comparable metrics.
- **Fresh validation** (optional): a reviewed subset of recent data can be used to measure current distribution.
- When Gold set is updated (Gold v2), re-evaluate older models on Gold v2 to compare fairly.

**Important**: Gold v1 is a fixed subset and must **not** be used for training.
All model comparisons (e.g., v15 vs new model) are done on the same fixed Gold set.

### Gold v1 creation
- Built once via `tools/make_gold_split.py`.
- Input: `data/commands_raw.md` + `data/none_seed.txt` (deduped by text).
- Stratified split per label with fixed seed (default: 42) and ratio (default: 80/20).
- Output:
  - `data/gold_v1.jsonl` (validation, frozen)
  - `data/train_base.jsonl` (train pool)

## Review Workflow (Lightweight)
- Review is **on-demand**; no weekly quota.
- Prioritize:
  1) Declined commands
  2) NONE examples
- Reviewed items move to **reviewed** bucket and can be included in training.

## Training Inputs
- **Train set**: `train_base.jsonl` + auto-train + reviewed.
- **Validation set**: Gold v1 (always, fixed).
- **Optional**: supplemental NONE data to reach target ratio (NONE = 7x command).
  - `none_seed.txt` is always fully included.
  - Online/extra NONE data is only used to reach the target ratio.
  - If the ratio is lowered, we only drop online/extra NONE samples (not the seed set).

## Model Versioning
- Baseline model: **v15**.
- New models: `v15_al_YYYYMMDD` (or similar).
- Promote only if metrics on Gold v1 improve or stay stable.

## Gold v2 (future)
- Gold v2 will replace Gold v1 **only** once we have enough high-quality reviewed data
  with a better NONE distribution.
- Candidate entries can be tracked via a `gold_candidate` flag on reviewed items,
  then promoted into a future `data/gold_v2.jsonl` when ready.

## Open Items (to implement)
- Storage format (likely JSONL) and folder layout.
- Review UI/CLI to label examples quickly.
- One-shot retrain script that:
  - builds train/val sets
  - trains model
  - reports metrics
  - outputs a versioned bundle

