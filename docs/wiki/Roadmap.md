# MetroSim26 roadmap

Updated October 8, 2026. This is a planning backlog, not a promise that every idea
will be implemented or a schedule. Feature work should be selected and tested in
small steps. The immediate priority is compatibility and a clear Blender workflow.

## Where we are

MetroSim26 already has a physical growth engine, saved yearly states, external
calculation processes, incremental Blender import, and a status/cancel panel.
Completed-run continuation now works, including the legacy Omaha output tested
through 2096. The reusable extension launcher restores saved settings and offers
a source-folder/end-year dialog; its Blender workflow is still being tested.

Optional named projects, phased openings, collector/access roads, bounded growth
feedback, event records, and evidence-based historical visibility already have
initial implementations. They are disabled by default and do not yet provide all
the richer behavior below. See the [feature guide](https://github.com/an-vu/metrosim26/blob/main/docs/evolution.md)
for current behavior and limits.

## Priority and sequencing

The phases below are ordered from first to later. Within each phase, items are
listed in a suggested working order. This is a dependency-aware plan, not a
commitment to finish every item before any later experiment. Existing features
remain marked as implemented or partial; their placement means stabilize or deepen
them, not rebuild them. Validation and compatibility checks continue in every phase.

**First integrated milestone (through phase 4):** generate a small neighborhood,
inspect why it grew, read its yearly history, clear/reset growth while retaining
the map, and compare a second simple scenario. Use this to prove the workflow before
adding economic cycles, disasters, or other large systems.

## Phase 1. Protect saved work and stabilize compatibility

First, make existing results dependable. Extend a legacy run and its extension, reconnect a moved folder, and recover supported interruptions without altering original results.

| Feature | Status and intended behavior |
| --- | --- |
| True continuation / EXTEND | Implemented for supported completed runs. Calculate only years after the saved end year; preserve original results. Continue strengthening compatibility rather than promising every past format works. |
| General extension launcher | Implemented; Blender testing ongoing. Restore original settings, detect saved scene linkage where available, or select a saved folder. A `.blend` alone is insufficient to continue calculations. |
| Portable project folders | Planned. Package the Blender file, saved simulation, settings, and history together; use relative links where possible and provide relocation/reconnection support. Moving drives should not break continuation. |
| Resume interrupted calculations | Planned. Recover cancelled or crashed calculations from validated checkpoints. This is separate from EXTEND, which currently requires a completed run; define recovery for simulation, geometry export, and import separately. |
| Model validation and uncertainty | Planned expansion of existing checks. Verify capacity accounting, geometry overlaps, deterministic behavior, and plausible growth patterns throughout development. Label assumptions and uncertainty; later compare against observed development when suitable evidence exists. |

## Phase 2. Make Blender clear and easy to use

Make the everyday workflow understandable before adding more simulation systems. A user should know what is running, change ordinary settings without code, and test changes quickly.

| Feature | Status and intended behavior |
| --- | --- |
| Clear overall progress | Basic phase/year/workers/cancel panel exists. Add explicit step numbering for preparation, simulation, geometry preparation, and import; show Ready only after import finishes. Use meaningful per-stage progress and an overall percentage only if it can be measured reasonably. |
| Responsive Blender execution | Implemented foundation: heavy work runs externally and scene import is incremental. Keep improving responsiveness as features grow. |
| Panel settings and add-on | Planned. New Simulation, Open Saved Run, Extend, ordinary scenario settings, and an Advanced section without requiring code edits. Eventually install once as a Blender add-on. |
| Small visual test scene | Planned. A quick, repeatable sample with housing, retail, industry, roads, and redevelopment. Review visual and workflow changes here before a full Omaha calculation. |

## Phase 3. Reuse the map and improve generated appearance

Address the costly rebuild and visual-quality pain points. Verify that reset operations affect only the intended generated content and preserve user work.

| Feature | Status and intended behavior |
| --- | --- |
| Clear generated growth | Planned. Remove simulated additions while retaining the imported base map and original buildings. Preserve user cameras, materials, and manual objects. Removing geometry must not silently change authoritative saved simulation data. |
| Start fresh using the existing map | Planned. Reset scenario state and reuse cached baseline data and imported base geometry where compatible, avoiding expensive repeated map import. Distinguish this from merely hiding/deleting growth in the scene. Handle generated roads/projects and baseline visualization restoration explicitly. |
| Building scale audit | First visual-quality task. Investigate the reported oversized generated buildings against nearby mapped buildings of similar use. Check units, footprint area, width/depth, heights, spacing, and capacity-to-floor-area conversion before changing defaults. Distinguish bugs from intended large building types; do not shrink everything uniformly. |
| Geometry inspection and cleanup | Planned before detailed materials. Investigate reported overlapping faces and distinguish duplicate/coplanar surfaces (including z-fighting) from intentional stacked geometry. Detect duplicate faces, degenerate geometry, and unintended intersections; provide a preview/report and scoped cleanup for MetroSim-generated objects. Preserve originals and manual objects, avoid indiscriminate vertex merging, and fix repeatable problems in the generator so regeneration stays clean. |
| Clean road intersections and surface joins | Planned alongside road-following layouts. Replace overlapping at-grade road surfaces with coherent junction geometry and consistent surface heights; join road/sidewalk/terrain edges appropriately. Preserve distinct bridge, tunnel, and other grade-separated surfaces rather than flattening every crossing into a junction. Validate both mapped-road visualization and generated roads without changing authoritative network connectivity as a cosmetic side effect. |
| Road-following site layouts | Planned after the scale audit. Orient and place buildings using local road direction, including curved frontage, with suitable setbacks and access. Avoid making every development follow the simulation grid. |
| Blocks, parcels, and buildings | Planned layout foundation. Separate calculation cells from visible blocks and parcels; fit buildings within usable sites and coordinate local streets across cell boundaries. Preserve protected geometry, access, and capacity constraints. |
| Building-type footprint diversity | Planned after layout foundations. Give homes, row houses, apartments, shops, offices, and warehouses appropriate sizes and arrangements. Add attached, L-shaped, stepped, and courtyard footprints where suitable. Four rectangular buildings around a plaza should be an intentional project type rather than a repeated default. |
| Visual quality milestone | Validate scale, road alignment, footprint diversity, spacing, heights, and neighborhood transitions in a small scene: a curved residential street, commercial strip, and industrial area. Then review a full-city result. Keep presentation-only variation separate from changes to modeled capacity or development geometry; decorative roofs alone will not fix site layout. |
| Road-level camera tours | Planned after basic scale/layout and surface cleanup. Start with a camera following a selected path, then connected road routes; expose direction, speed, camera height, and smooth look-ahead. Freeze the city at a selected simulation year in a separate tour scene or equivalent independent time control, since simulation frames currently select years. Handle road elevation and lateral offset; automatic point-to-point routing depends on reliable network connectivity. Vehicle traffic and driving physics are outside the initial scope. Use tours for inspection as well as presentation. |
| Materials and realistic surfaces | Planned after geometry cleanup and stable surface categories. Make roads read as asphalt, lakes/rivers as water, green areas as grass, and buildings as appropriate facade/roof materials. Establish consistent texture scale, normals, UV or procedural mapping, and clear surface boundaries; test a small scene before city-wide rollout. Preserve user material edits, offer lightweight viewport settings, and keep visual assets separate from authoritative simulation data. Detailed vegetation, markings, and street dressing can follow once performance is understood. |

## Phase 4. Explain the city and compare simple alternatives

Build the first complete small experience: generate a neighborhood, inspect why it grew, read its yearly history, reset it, and compare another scenario. Start branching with existing growth assumptions; themed scenarios wait for their underlying systems.

| Feature | Status and intended behavior |
| --- | --- |
| Yearly explanation report | Planned. Explain annual demand, vacancy absorption, infill, new development, constraints, and unmet demand using saved numbers and decisions. Connect “what happened” to “why growth changed.” |
| Site-level explanations | Planned. Select development to see its year, ranking factors, access/employment context, constraints, and recorded reasons for selection. Capture decision evidence during simulation; older runs may only support partial explanations. Never invent missing causes. |
| Building/project inspector | Planned. Click a generated building or project in Blender to see its name, construction year, capacity, project membership, and recorded reason for appearing. Connect it to site explanations and yearly history; disclose missing evidence in older runs. |
| City event / narrative engine | Initial event histories exist. Build a readable chronicle of project starts, road openings, expansions, decline, and redevelopment, tied to modeled events. |
| Scenario branching | Planned. Start from the same baseline or a supported saved state and compare Baseline, High Growth, Transit Boom, Climate Stress, Tech Boom, Recession/Decline, or AI Shock. Current EXTEND preserves scenario settings; changing assumptions needs a separate branching design. |
| Compare scenarios | Planned. Show differences in housing, jobs, vacancy, developed land, and infrastructure, alongside map changes. Start with compatible baselines, matching years, and a small set of assumptions; identify incompatible comparisons. |

## Phase 5. Give development identity and visible history

Deepen existing optional project foundations after the basic experience works. Keep retrofitted presentation separate from authoritative calculation history.

| Feature | Status and intended behavior |
| --- | --- |
| Named fictional projects | Initial deterministic naming and project records exist. Develop stronger identities for malls, neighborhoods, industrial parks, campuses, and mixed-use districts. Clearly label fictional content. |
| Multi-year construction | Initial phased openings and capacity accounting exist. Expand toward proposal, construction, partial opening, completion, expansion, and redevelopment, with suitable visuals. |
| Neighborhood identity | Planned. Persistent district character and history, with more visual variety than generic blocks. |
| Projectization of finished runs | Planned. Add names, morphology, phases, and presentation history to existing results without rerunning core growth. Preserve authoritative yearly capacity and geometry constraints; changes to actual opening dates or capacity require a new scenario rather than a cosmetic retrofit. |
| Rare landmarks | Planned. Occasional stadiums, towers, civic centers, parks, bridges, and museums that meaningfully change the city's appearance. |

## Phase 6. Connect infrastructure, jobs, and growth

Develop and test one cause/effect chain at a time. Validate the actual model effects before describing them in the narrative; advanced corridors follow the necessary routing and demand foundations.

| Feature | Status and intended behavior |
| --- | --- |
| Cause/effect chains | A central direction: employer opens → jobs rise → housing demand rises → neighborhood grows → road pressure rises → corridor opens → retail follows. Effects must pass through model state rather than independent random narrative events. |
| Major employers/institutions | Planned beyond current project archetypes. Campuses, hospitals, universities, and logistics hubs whose openings, expansions, and closures affect jobs and surrounding demand. |
| Infrastructure affects growth | Initial bounded accessibility effects exist. Deepen the relationship between corridor openings and subsequent housing, retail, and employment location choices. |
| Traffic/congestion pressure | Planned. Coarse demand/capacity pressure sufficient to justify infrastructure decisions; not microscopic traffic simulation. Existing poor-access pressure is not a congestion model. |
| Major infrastructure evolution | Collector/access-road foundations exist. Expand to arterials, widening, highway access/interchanges, bridges, and transit corridors with appropriate geometry and constraints. |
| Road-network growth review | Review local site roads and optional collector/access corridors over a long run. Verify connections, opening years, cross-site continuity, and whether demand/poor-access triggers produce useful network growth. Identify disconnected or repetitive layouts before adding larger road types. New roads should respond to development and pressure, not a fixed annual quota. |
| Transit | Planned. BRT, light rail, commuter corridors, and station-driven density. |

## Phase 7. Add economic, lifecycle, and policy scenarios

Use the branching and comparison tools to evaluate each new mechanism independently before combining them. These are later systems, not random narrative events.

| Feature | Status and intended behavior |
| --- | --- |
| Land value/desirability | Planned. Dynamic economic pressure influencing location choice and redevelopment beyond existing spatial scores. |
| Economic cycles | Planned. Booms, recessions, recovery, vacancy changes, and construction slowdowns with measurable effects. |
| Building aging and lifecycle | Planned. Age-dependent renovation, decline, demolition, and redevelopment beyond current redevelopment rules. |
| Project failure | Planned. Stalled subdivisions, failing malls, and vacant offices with actual capacity, demand, and vacancy consequences. |
| Policy scenarios | Planned. Upzoning, growth boundaries, industrial incentives, transit-oriented development, and park protection. |
| Annexation/municipal boundaries | Planned. Represent municipal changes over time and define their effects on growth and policy. |

## Phase 8. Expand historical and environmental evidence

Broader reconstruction needs real source coverage. Establish exposure data before hazard-specific consequences; validate against observed history where possible. Evidence collection can begin earlier without blocking the core workflow.

| Feature | Status and intended behavior |
| --- | --- |
| Historical reconstruction before 2026 | Initial evidence-based visibility/timeline support exists. Expand toward a defensible 2006–2026 reconstruction, connected to future simulation; do not reverse the future growth model or imply unknown history is known. |
| Climate/flood layer | Planned. Real floodplain/elevation evidence to influence development constraints, cost, and exposure. |
| Disasters and shocks | Planned. Floods, tornadoes, pandemics, employer closures, and other shocks with downstream consequences, not text-only events. |

## Phase 9. Expand reach and presentation

After the local workflow and saved formats are stable, broaden supported locations and publishing. Streaming is an optional performance/experience project, not a prerequisite for a usable simulator.

| Feature | Status and intended behavior |
| --- | --- |
| Broader location support | Planned. Generalize data acquisition and baseline assumptions before presenting arbitrary cities as supported. Verify data availability and model suitability for each supported area. |
| Website timeline | Planned. Export yearly states, events, projects, and provenance for an interactive MetroSim website. Existing files are a foundation, not a finished website or public export contract. |
| Live simulation streaming | Planned. Show years in Blender while calculation continues, rather than waiting for the complete geometry export. |

## Principles for future work

- Keep original runs intact and version saved formats deliberately.
- Separate simulation changes from visualization-only improvements.
- Reuse compatible baseline data and geometry, but invalidate stale results when
  relevant inputs change.
- Explain real model decisions; mark fictional projects and uncertain history clearly.
- Treat the three long-term themes as connected: project/narrative identity,
  infrastructure with consequences, and an evidence-based, extendable timeline.
