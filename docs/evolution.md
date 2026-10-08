# Optional city-history layers

These additions are disabled by default. They run in the external calculation
process and reuse the existing demand model, site planner, worker broker, annual
checkpoints, geometry lifetimes, and incremental Blender importer.

## Enable future projects and infrastructure

In `metrosim26/config.py`:

```python
ENABLE_PROJECTS = True
ENABLE_INFRASTRUCTURE = True
RUN_MODE = "SIMULATE"
```

Prefetch and launch exactly as before. Neither feature downloads additional data.
With both flags off and historical mode disabled, scenario identity and authoritative
simulation outputs remain compatible with the previous engine. Enabled layers have
their own versioned configuration identity.

### Named development

`metrosim26/projects.py` promotes sufficiently large **demand-selected, geometry-validated
site plans** into fictional projects. It does not schedule a mall at a fixed date or
choose a random location. The normal ranking/archetype rules still select development.

The initial project types are residential neighborhoods, regional retail centers,
industrial parks, office campuses, institutional expansions, mixed-use districts,
and downtown complexes. A project initially covers one existing planning cell;
this is not yet a multi-cell master-plan optimizer. Names derive deterministically
from the seed and project identity and are explicitly marked fictional.

Defaults require at least two selected buildings and either 40 homes or 150 jobs,
with at most three new projects per year. Small actions remain anonymous. These
thresholds are editable in `make_config()`; archetypes with fewer accepted buildings
may need a lower building threshold to qualify.

- The start year establishes the project and its existing site-plan access roads.
- Selected buildings and their parking open over up to three following years.
- Only opened buildings add housing/job capacity. Committed phases finish even if
  other development has since satisfied demand; excess capacity remains vacant.
- Pending cells cannot receive competing infill or redevelopment. Ordinary infill
  resumes afterward; another qualifying addition becomes a phased expansion.
- Normal redevelopment can retire a completed project. The existing replacement
  rules remain authoritative; this first layer does not phase demolition/rebuilding.

Construction dates do not get compressed at `END_YEAR`. A run can finish with a
project still under construction and its remaining plans saved. Committed openings
take priority over new discretionary actions, including their annual action limit.

Completed projects influence adjacent housing, employment, and relevant sector
signals. Each annual calculation starts from the original baseline signals; the
total boost is capped at 0.15 per signal/cell and the final signal at 1.0. There is
no recursive compounding. Centrality and worker geometry inputs remain fixed.

### Major infrastructure

`metrosim26/infrastructure.py` initially supports **collector extensions and major access
roads**. It preserves existing OSM roads and procedural local site roads. Widening,
bridges, transit, ramps, and interchanges are deferred; highway/bridge crossings are
blocked rather than silently treated as ordinary at-grade junctions.

Candidates need sustained poor access near developed cells or named projects,
plus unmet household/job demand or a committed named project. Defaults require
three pressure years and access below 0.40, with at most one new corridor per year
and 24 per run. These are explicit scenario assumptions, not traffic forecasts.

Routing uses a bounded four-neighbor grid search, up to eight steps. It starts at
a mapped road endpoint, an open generated major-road segment, or a generated local
road junction. Turns and misalignment with local road orientation cost extra.
Full-width corridor polygons, including segment caps, are checked against study
bounds, OSM buildings, protected water/parks, generated buildings, parking, and
committed project geometry. A blocked route is skipped; no demolition or bridge
is invented to force a connection. Coarse routing can miss a feasible real corridor.

The first implementation conservatively reserves **whole intersected cells** from
later discretionary development. Existing buildings remain intact and previously
committed, nonintersecting phases can finish. This reduces developable land more
than a parcel-accurate right-of-way model would.

Corridors are proposed first, enter construction the next year, and open segments
from the following year onward. Only open segments raise neighboring accessibility:
at most +0.20 over the original baseline, capped at 1.0. The gain does not stack with
additional corridors. This affects future candidate eligibility/ranking; no vehicle
flows, travel times, budgets, or engineering capacity are modeled.

Blender separates **Observed Roads**, **Scenario Local Roads**, and **Scenario
Infrastructure (Fictional)**. Major-road surfaces appear in their segment opening
years. Named project objects carry fictional names/IDs and provenance properties.

## Historical evidence and an earlier timeline

```python
HISTORICAL_MODE = "EVIDENCE"
TIMELINE_START_YEAR = 2006
HISTORICAL_EVIDENCE_FILE = "historical_evidence.json"
```

A relative evidence path is resolved from the repository root, beside `blender.py`. No historical data
is bundled or fetched. Leave the filename empty for an explicitly **unknown** past:
undated features first become visible at the baseline year, not at an invented date.
An empty historical scene does **not** mean those buildings or roads were absent.

The first evidence adapter accepts dates for surviving baseline features and sourced
historical events. Example **schema illustration**, not a real Omaha assertion:

```json
{
  "schema_version": 1,
  "features": [
    {
      "kind": "building",
      "id": "REPLACE_WITH_AN_ACTUAL_BASELINE_ID",
      "start_year": 2010,
      "source": "REPLACE_WITH_YOUR_EVIDENCE_CITATION",
      "provenance": "historically_reconstructed"
    }
  ],
  "events": []
}
```

Feature kinds are `building`, `road`, and `land`; IDs must match saved baseline IDs.
Each feature needs an integer start year no later than the baseline, a source,
and provenance `observed` or `historically_reconstructed`. Duplicate IDs, unknown
features, unsupported top-level fields, and demolition intervals are rejected.
Events require a pre-baseline `year`, `source`, and the same provenance choices;
use `name` and `action` for readable timeline descriptions.

Historical yearly states are always labeled `historically_reconstructed`. Their
development/use grids are `model_derived` from visible evidence-backed building
geometry; coverage flags and unknown-feature counts accompany them. Feature dates
never establish complete cell-wide historical coverage: `unknown_coverage` stays
true, while `evidence_building_coverage` marks cells with visible evidence-backed
buildings. Current OSM
footprints are proxies, not reconstructed historical shapes. Demolished buildings,
historical population/employment, past road geometry, and land-cover raster evidence
need future adapters; this code does not fabricate them or reverse future growth.

`metrosim26/history.py` is the evidence-to-state boundary. Blender receives the same
lifetime representation regardless of source. Frame mapping is
`year - timeline_start_year + 1`: with 2006 as the start, 2026 is frame 21 and 2076
is frame 71. Future demand still starts at `BASE_YEAR`, independently of that mapping.
Evidence content is hashed into run identity; keep the evidence file available for
AUTO/REPLAY. Scene properties describe the historical uncertainty and fictional future.

## Files and continuation

Within the completed run's `simulation/` directory:

| File | Purpose |
| --- | --- |
| `projects.json` | Project records, delivered capacity, and committed future site phases |
| `infrastructure.json` | Corridors, segment opening years, connections, and pressure counters |
| `events.json` | Unified sourced historical / fictional future event history |
| `history.json`, `history/<year>.npz` | Historical evidence lifetimes, yearly visibility, grids, and uncertainty |
| `continuation.json` | Versioned complete-year continuation contract |

Files are written only for the relevant enabled features. Existing annual future
state files and `versions.json` remain authoritative; optional state fields record
project status, infrastructure status, and provenance. Complete-run checksums cover
the additional outputs, and cancelled jobs retain completed annual checkpoints.

Future layers automatically write continuation contracts. Set `SAVE_CONTINUATION = True`
to export one for an otherwise ordinary SIMULATE run. The contract references hashed
baseline/state/version files and records households/jobs, free capacity, unmet and
cumulative demand, capacity additions, active versions (including redevelopment ages),
project commitments, infrastructure pressure, and all geometry needed to rebuild
reservations and bounded feedback. `read_continuation()` validates the bundle.

**EXTEND is supported for completed compatible runs.** Set `RUN_MODE = "EXTEND"`,
`EXTEND_FROM` to the source run (or its `simulation/` directory), and a later
`END_YEAR`. All other model settings must match, including optional layers and
historical evidence. Source checksums are verified before restoration. New contracts
declare `extension_execution_supported: true`; validated older contracts remain
readable. Ordinary schema-3 Omaha runs can reconstruct the ledger without a contract.

The loader restores the last annual grids, active versions, cumulative demand and
capacity, committed project phases, and infrastructure pressure/reservations. Planning
caches are rebuilt lazily; earlier years are copied, not recalculated. Historical
states are retained. Extension writes an isolated result and preserves the source.
Tests compare split versus uninterrupted yearly files, versions, metrics, events,
and geometry, including repeated extensions and spawned planning workers.

Blender builds a new scene containing the full extended timeline. Updating an existing
scene in place and resuming an interrupted partial run remain unsupported. Keep the
original `EXTEND_FROM` and new `END_YEAR` when switching to REPLAY. To extend a second
time, point `EXTEND_FROM` at the completed extension. See README for examples.

Run the offline tests with `python -m unittest discover -s tests -v`. They cover
deterministic projects/phases, serial versus spawned-worker outputs, capacity ledgers,
road connections and exclusions, opening-year effects, historical provenance and
lifetimes, continuation integrity, and unchanged outputs with features disabled.
