
| kind | agent | n | baseline mean (jobs/min) | baseline SD | TR mean | TR SD | TR CV | fire tick mean (s) | fire tick SD (s) | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| belt_cut | noop | 10 | 480.5 | 0.88 | 0.1095 | 0.0004 | 0.0038 | 60.9 | 0.24 | 0 |
| belt_cut | oracle | 3 | 480.7 | 0.54 | 0.9986 | 0.0011 | 0.0011 | 60.8 | 0.28 | 0 |
| entity_destruction | noop | 10 | 480.5 | 0.85 | 0.7451 | 0.0333 | 0.0448 | 60.8 | 0.26 | 0 |
| entity_destruction | oracle | 3 | 480.4 | 0.59 | 0.9992 | 0.0012 | 0.0012 | 60.8 | 0.29 | 0 |

per-run:
  jitter_J1_belt_cut_noop_00: base=479.9 TR=0.1094 raw=0.1094 fire=61010 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_01: base=480.0 TR=0.1094 raw=0.1094 fire=61000 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_02: base=482.0 TR=0.1100 raw=0.1100 fire=60500 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_03: base=479.9 TR=0.1094 raw=0.1094 fire=60510 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_04: base=479.9 TR=0.1094 raw=0.1094 fire=61010 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_05: base=481.0 TR=0.1091 raw=0.1091 fire=61000 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_06: base=480.0 TR=0.1094 raw=0.1094 fire=61000 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_07: base=480.0 TR=0.1094 raw=0.1094 fire=61000 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_08: base=482.1 TR=0.1089 raw=0.1089 fire=60500 recovered=False ttr=120000.0
  jitter_J1_belt_cut_noop_09: base=480.0 TR=0.1104 raw=0.1104 fire=61000 recovered=False ttr=120000.0
  jitter_J1_belt_cut_oracle_00: base=480.1 TR=0.9999 raw=0.9999 fire=61000 recovered=True ttr=30504.0
  jitter_J1_belt_cut_oracle_01: base=481.0 TR=0.9980 raw=0.9980 fire=60508 recovered=True ttr=30993.0
  jitter_J1_belt_cut_oracle_02: base=481.0 TR=0.9979 raw=0.9979 fire=61001 recovered=True ttr=31000.0
  jitter_J1_entity_destruction_noop_00: base=480.0 TR=0.7499 raw=0.7499 fire=60501 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_01: base=480.0 TR=0.7520 raw=0.7520 fire=61001 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_02: base=480.1 TR=0.7519 raw=0.7519 fire=61000 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_03: base=481.1 TR=0.7670 raw=0.7670 fire=61000 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_04: base=479.9 TR=0.7334 raw=0.7334 fire=61007 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_05: base=480.0 TR=0.7698 raw=0.7698 fire=60508 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_06: base=482.0 TR=0.7770 raw=0.7770 fire=60500 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_07: base=482.0 TR=0.7665 raw=0.7665 fire=60500 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_08: base=480.0 TR=0.6645 raw=0.6645 fire=61003 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_noop_09: base=480.0 TR=0.7187 raw=0.7187 fire=61007 recovered=False ttr=120000.0
  jitter_J1_entity_destruction_oracle_00: base=480.1 TR=0.9998 raw=0.9998 fire=60503 recovered=True ttr=30498.0
  jitter_J1_entity_destruction_oracle_01: base=480.0 TR=1.0000 raw=1.0000 fire=61002 recovered=True ttr=30507.0
  jitter_J1_entity_destruction_oracle_02: base=481.0 TR=0.9978 raw=0.9978 fire=61002 recovered=True ttr=30503.0
