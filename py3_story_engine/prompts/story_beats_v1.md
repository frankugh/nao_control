# Story Beats V1

Deze documentatie beschrijft de vaste beat-volgorde en transitieregels voor de story mode.

## Beat volgorde

1. `setup`
2. `trigger`
3. `plan`
4. `complication`
5. `reversal`
6. `dark_moment`
7. `climax`
8. `aftermath`

## Harde regels

1. Beats verlopen strikt in volgorde.
2. Geen skippen van beats.
3. Transitie is alleen `stay` of `next`.
4. Beat-engine in code beslist of `next` echt wordt toegepast.
5. `phase` moet altijd gelijk zijn aan `beats[beat_idx]`.
6. Storyteller en StateUpdater output moet in het Nederlands zijn.

## Korte constraints per beat

## setup
- Introduceer wereld, toon, personages.
- Geen grote ontknoping of climax.

## trigger
- Introduceer de aanleiding of verstoring.
- Nog geen volledige oplossing.

## plan
- Maak intentie of plan concreet.
- Nog geen finale botsing.

## complication
- Verhoog frictie of obstakels.
- Geen complete afronding.

## reversal
- Voeg een betekenisvolle wending toe.
- Houd spanning op richting eindfase.

## dark_moment
- Laagste punt, twijfel, verlies of impasse.
- Geen echte overwinning.

## climax
- Beslissende actie of confrontatie.
- Centrale conflict bereikt hoogtepunt.

## aftermath
- Gevolgen en afronding.
- Geen nieuwe grote conflictboog starten.

## State richtlijnen

- `global_summary`: kort, stabiel, 4-6 zinnen.
- `last_scene_summary`: 1-2 zinnen.
- `pinned_facts`: maximaal 12.
- `active_quests`: maximaal 10.
- `relationships`: maximaal 12.
- `player_traits`: maximaal 5.
- `player_inventory`: maximaal 5.
- `threads`: gebruik unieke IDs, max 12, sluit expliciet af met `resolve`.

## Conservative Beat Gates (`conservative_v1`)

Code gebruikt updater-signalen + user-commit signalen om `next` wel/niet toe te staan.
Als criteria niet gehaald worden, forceert code `stay` (met debug melding).

Signalen:
- `S_FACT_ADD`: `facts.add` niet leeg
- `S_QUEST_ADD`: `active_quests.add` niet leeg
- `S_QUEST_REMOVE`: `active_quests.remove` niet leeg
- `S_THREAD_OPEN`: `threads.open` niet leeg
- `S_THREAD_RESOLVE`: `threads.resolve` niet leeg
- `S_USER_COMMIT`: user input is keuze (`1`/`2`) of expliciete commit-actie
- `S_OBSTACLE`: obstakelwoorden in `last_scene_summary` of `facts.add`
- `S_REVERSAL`: wending-woorden in `last_scene_summary` of `facts.add`
- `S_SETBACK`: verlies/impasse-woorden in `last_scene_summary` of `facts.add`

Overgangscriteria:
1. `setup -> trigger`: `S_FACT_ADD OR S_QUEST_ADD OR S_THREAD_OPEN`
2. `trigger -> plan`: `(S_QUEST_ADD OR S_THREAD_OPEN) AND S_USER_COMMIT`
3. `plan -> complication`: `S_FACT_ADD AND S_OBSTACLE`
4. `complication -> reversal`: `S_FACT_ADD AND S_REVERSAL`
5. `reversal -> dark_moment`: `(S_FACT_ADD OR S_QUEST_REMOVE OR S_THREAD_RESOLVE) AND S_SETBACK`
6. `dark_moment -> climax`: `(S_FACT_ADD OR S_QUEST_ADD OR S_THREAD_OPEN) AND S_USER_COMMIT`
7. `climax -> aftermath`: `S_THREAD_RESOLVE`
