# xschem-skill

A Claude Code skill for reading, evaluating, drawing and editing [xschem](https://xschem.sourceforge.io/)
schematics (`.sch`) and symbols (`.sym`) with low context overhead and verified connectivity.
It targets the [IIC-OSIC-TOOLS](https://github.com/iic-jku/IIC-OSIC-TOOLS) environment (IHP sg13g2).

- `xs read` gives a compact summary: ports, instances with their parameters and pin→net maps, plus warnings.
- `xs edit` is a batch edit language that works with pin references (`wire M1.D M3.D`,
  `place M2 sg13_lv_nmos M1.D+0,120 anchor=D`). It reports the resulting connectivity and any new warnings.
- `xs mksym`, symbol pin editing, `xs netlist`, `xs render` (PNG), and `xs xcheck`, which cross-checks
  connectivity against xschem itself.

Reading and editing are pure Python (stdlib only) and run on the host. Netlisting and rendering run
xschem headless inside a distrobox container (default name `iic-osic-tools2`; override with `XS_CONTAINER`),
or directly when the tool itself runs inside the container.

## Install

```sh
git clone git@github.com:Fundacion-Fulgor/xschem-skill.git ~/.claude/skills/xschem
```

See [SKILL.md](SKILL.md) for usage and [reference.md](reference.md) for common symbols.
