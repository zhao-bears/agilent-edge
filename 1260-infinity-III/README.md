# 1260 Infinity III edge

PUDA edge service for an Agilent 1260 Infinity III LC. Runs on the OpenLab CDS PC and submits methods through [moses-hplc](https://pypi.org/project/moses-hplc/) (`moses.agilent.AgilentController`).

The modules have no open protocol. This process has to sit on the Windows workstation where OpenLab and the Agilent automation assemblies are installed. `moses.agilent` uses `pythonnet`, which does not support Python 3.14, so this service is pinned to Python 3.13. A Linux container cannot load those Windows assemblies.

## What it exposes

| Command | Effect |
| --- | --- |
| `read_status` | OpenLab instrument state. Does not move solvent. |
| `set_on` | OpenLab set-to-on. |
| `standby` | Park on the standby method. Modules stay powered. |
| `shutdown` | Same park. Moses cannot power the stack off. |
| `run_gradient` | Write a binary gradient into the template `.amx` and inject one sample. |
| `run_method` | Inject with an existing `.amx`. |

Live state is published on `puda.<MACHINE_ID>.tlm.stream.status`.

## Setup

Use the Python 3.13 already installed on the OpenLab PC.

```bat
python -m pip install -e .
copy .env.example .env
```

Edit `.env`:

- `MACHINE_ID` — `1260-infinity-iii` unless this lab already uses that id.
- `NATS_SERVERS` — bears NATS URLs.
- `AGILENT_SDK_DIR` — directory that contains both `OpenLab Acquisition` and `OpenLab Services`. Moses looks there for `Agilent.OpenLab.RawData.dll`, `Agilent.OpenLab.Storage.dll`, and `Agilent.OpenLab.SharedServices.dll`.
- `AGILENT_AUTOMATION_CORE` — directory containing `Agilent.OpenLAB.Acquisition.AutomationCore.dll`.
- `AGILENT_AUTOMATION_INSTRUMENT` — directory containing `Agilent.OpenLAB.Acquisition.AutomationInstrument.dll`.
- `INSTRUMENT_CONFIG_PATH` — `config.json` in this directory.

In that JSON:

- `connection_string` is the OpenLab Shared Services connection string for this workstation.
- `instrument_id` and `project_id` may stay `null`; Moses then takes the first instrument and project it finds.
- `eluents` maps pump channels `A1`, `A2`, `B1`, and `B2` to the solvent names used in gradients. Moses sends the second character of the key as the channel number.
- `default_methods.positive` is the LC template `.amx`, despite the name. `smx` is the pretreatment file. Every `run_gradient` writes `equilibration_time` into that file, including 0. `pmx` is the processing method. Add `negative` or `positive_negative` only if this stack has a mass detector.
- `offset_volume` stays `null` on an LC-only system.

`MAX_FLOW_ML_MIN` defaults to 5, the settable maximum of the 1260 Infinity III binary pump (G7112B). Raise it only when the installed pump's manual allows more, such as a quaternary pump. `MAX_INJECTION_UL` defaults to 100 for a standard vialsampler or multisampler metering head.

## Run

From this directory, on the OpenLab PC:

```bat
python main.py
```

Or `start_edge.bat`.

Check the OpenLab connection without starting the edge or moving solvent. Acquisition should already be online:

```bat
python tests/manual_status.py
```

A successful check prints `{"status": "Idle"}` or another OpenLab state such as `Running`.

Check the edge from a machine on the same NATS network:

```bash
puda machine info 1260-infinity-iii
puda machine watch 'puda.1260-infinity-iii.tlm.stream.>'
```

## Gradient

`run_gradient` takes the same object as `LCGradient.from_dict`. Solvent B is a fraction from 0 to 1.

```json
{
  "name": "10min",
  "eluents": {
    "solvent_a": "Water (0.1% Formic Acid)",
    "solvent_b": "Acetonitrile (0.1% Formic Acid)"
  },
  "run_time": 10.0,
  "equilibration_time": 1.0,
  "header_gradient_table": ["time", "flow_rate", "Acetonitrile (0.1% Formic Acid)"],
  "gradient_table": [
    [0.0, 1.0, 0.05],
    [1.0, 1.0, 0.05],
    [8.0, 1.0, 0.95],
    [10.0, 1.0, 0.05]
  ]
}
```

`sample_position` is forwarded unchanged. Use the address OpenLab shows for the installed sampler: a vial address on a vialsampler, or a drawer address such as `D1F-A1` on a multisampler.
