# This file is Copyright (c) 2014-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2020 Antmicro <www.antmicro.com>
# License: BSD

import os
import subprocess
import sys
import math
from typing import NamedTuple, Union, List
import re

from migen.fhdl.structure import _Fragment, wrap, Constant
from migen.fhdl.specials import Instance

from litex.build.generic_platform import *
from litex.build import tools


def _unwrap(value):
    return value.value if isinstance(value, Constant) else value

# Constraints (.xdc) -------------------------------------------------------------------------------

def _xdc_separator(msg):
    r =  "#"*80 + "\n"
    r += "# " + msg + "\n"
    r += "#"*80 + "\n"
    return r


def _format_xdc_constraint(c):
    if isinstance(c, Pins):
        return "set_property LOC " + c.identifiers[0]
    elif isinstance(c, IOStandard):
        return "set_property IOSTANDARD " + c.name
    elif isinstance(c, Drive):
        return "set_property DRIVE " + str(c.strength)
    elif isinstance(c, Misc):
        return "set_property " + c.misc.replace("=", " ")
    elif isinstance(c, Inverted):
        return None
    else:
        raise ValueError("unknown constraint {}".format(c))


def _format_xdc(signame, resname, *constraints):
    fmt_c = [_format_xdc_constraint(c) for c in constraints]
    fmt_r = resname[0] + ":" + str(resname[1])
    if resname[2] is not None:
        fmt_r += "." + resname[2]
    r = "# {}\n".format(fmt_r)
    for c in fmt_c:
        if c is not None:
            r += c + " [get_ports {" + signame + "}]\n"
    r += "\n"
    return r


def _build_xdc(named_sc):
    r = ""
    for sig, pins, others, resname in named_sc:
        if len(pins) > 1:
            for i, p in enumerate(pins):
                r += _format_xdc(sig + "[" + str(i) + "]", resname, Pins(p), *others)
        elif pins:
            r += _format_xdc(sig, resname, Pins(pins[0]), *others)
        else:
            r += _format_xdc(sig, resname, *others)
    return r

# PCF ----------------------------------------------------------------------------------------------

def _build_pcf(named_sc):
    r = ""
    current_resname = ""
    for sig, pins, _, resname in named_sc:
        if current_resname != resname[0]:
            if current_resname:
                r += "\n"
            current_resname = resname[0]
            r += f"# {current_resname}\n"
        if len(pins) > 1:
            for i, p in enumerate(pins):
                r += f"set_io {sig}[{i}] {Pins(p).identifiers[0]}\n"
        elif pins:
            r += f"set_io {sig} {Pins(pins[0]).identifiers[0]}\n"
    return r

# SDC ----------------------------------------------------------------------------------------------

def _build_sdc(named_pc):
    return "\n".join(named_pc) if named_pc else ""

# Makefile -----------------------------------------------------------------------------------------

class _MakefileGenerator:
    class Var(NamedTuple):
        name: str
        value: Union[str, List[str]] = ""

    class Rule(NamedTuple):
        target: str
        prerequisites: List[str] = []
        commands: List[str] = []
        phony: bool = False

    def __init__(self, ast):
        self.ast = ast

    def generate(self):
        makefile = []
        for entry in self.ast:
            if isinstance(entry, str):
                makefile.append(entry)
            elif isinstance(entry, self.Var):
                if not entry.value:
                    makefile.append(f"{entry.name} :=")
                elif isinstance(entry.value, list):
                    indent = " " * (len(entry.name) + len(" := "))
                    line = f"{entry.name} := {entry.value[0]}"
                    for value in entry.value[1:]:
                        line += " \\"
                        makefile.append(line)
                        line = indent + value
                    makefile.append(line)
                elif isinstance(entry.value, str):
                    makefile.append(f"{entry.name} := {entry.value}")
                else:
                    raise
            elif isinstance(entry, self.Rule):
                makefile.append("")
                if entry.phony:
                    makefile.append(f".PHONY: {entry.target}")
                makefile.append(" ".join([f"{entry.target}:", *entry.prerequisites]))
                for cmd in entry.commands:
                    makefile.append(f"\t{cmd}")

        return "\n".join(makefile)


def _run_make():
    if tools.subprocess_call_filtered("make", []) != 0:
        raise OSError("Subprocess failed")

# SymbiflowToolchain -------------------------------------------------------------------------------

class SymbiflowToolchain:
    attr_translate = {
        "keep":            ("dont_touch", "true"),
        "no_retiming":     ("dont_touch", "true"),
        "async_reg":       ("async_reg",  "true"),
        "mr_ff":           ("mr_ff",      "true"), # user-defined attribute
        "ars_ff1":         ("ars_ff1",    "true"), # user-defined attribute
        "ars_ff2":         ("ars_ff2",    "true"), # user-defined attribute
        "no_shreg_extract": None
    }

    def __init__(self):
        self.clocks = dict()
        self.false_paths = set()
        self.symbiflow_device = None
        self.bitstream_device = None

    def _check_properties(self, platform):
        if not self.symbiflow_device:
            raise ValueError(f"symbiflow_device is not specified")
        if not self.bitstream_device:
            try:
                self.bitstream_device = {
                    "xc7a": "artix7"
                }[platform.device[:4]]
            except KeyError:
                raise ValueError(f"Unsupported device: {platform.device}")

    def _generate_makefile(self, platform, build_name):
        Var = _MakefileGenerator.Var
        Rule = _MakefileGenerator.Rule

        makefile = _MakefileGenerator([
            "# Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\n",
            Var("TOP", build_name),
            Var("PARTNAME", platform.device),
            Var("DEVICE", self.symbiflow_device),
            Var("BITSTREAM_DEVICE", self.bitstream_device),
            "",
            Var("VERILOG", [f for f,language,_ in platform.sources if language in ["verilog", "system_verilog"]]),
            Var("MEM_INIT", [f"{name}" for name in os.listdir() if name.endswith(".init")]),
            Var("PCF", f"{build_name}.pcf"),
            Var("SDC", f"{build_name}.sdc"),
            Var("XDC", f"{build_name}.xdc"),
            Var("ARTIFACTS", [
                    "$(TOP).eblif", "$(TOP).frames", "$(TOP).ioplace", "$(TOP).net",
                    "$(TOP).place", "$(TOP).route", "$(TOP)_synth.*",
                    "*.bit", "*.fasm", "*.json", "*.log", "*.rpt",
                    "constraints.place"
                ]),

            Rule("all", ["$(TOP).bit"], phony=True),
            Rule("$(TOP).eblif", ["$(VERILOG)", "$(MEM_INIT)", "$(XDC)"], commands=[
                    "synth -t $(TOP) -v $(VERILOG) -d $(BITSTREAM_DEVICE) -p $(PARTNAME) -x $(XDC) > /dev/null"
                ]),
            Rule("$(TOP).net", ["$(TOP).eblif", "$(SDC)"], commands=[
                    "pack -e $(TOP).eblif -d $(DEVICE) -s $(SDC) > /dev/null"
                ]),
            Rule("$(TOP).place", ["$(TOP).net", "$(PCF)"], commands=[
                    "place -e $(TOP).eblif -d $(DEVICE) -p $(PCF) -n $(TOP).net -P $(PARTNAME) -s $(SDC) > /dev/null"
                ]),
            Rule("$(TOP).route", ["$(TOP).place"], commands=[
                    "route -e $(TOP).eblif -d $(DEVICE) -s $(SDC) > /dev/null"
                ]),
            Rule("$(TOP).fasm", ["$(TOP).route"], commands=[
                    "write_fasm -e $(TOP).eblif -d $(DEVICE) > /dev/null"
                ]),
            Rule("$(TOP).bit", ["$(TOP).fasm"], commands=[
                    "write_bitstream -d $(BITSTREAM_DEVICE) -f $(TOP).fasm -p $(PARTNAME) -b $(TOP).bit > /dev/null"
                ]),
            Rule("clean", phony=True, commands=[
                    "rm -f $(ARTIFACTS)"
                ]),
        ])

        tools.write_to_file("Makefile", makefile.generate())

    def _build_clock_constraints(self, platform):
        for clk, (period, phase) in sorted(self.clocks.items(), key=lambda x: x[0].duid):
            rising_edge = math.floor(period/360.0 * phase * 1e3)/1e3
            falling_edge = math.floor(((rising_edge + period/2) % period) * 1.e3)/1e3
            platform.add_platform_command(f"create_clock -period {period} {{clk}} -waveform {{{{{rising_edge} {falling_edge}}}}}", clk=clk)
        for from_, to in sorted(self.false_paths, key=lambda x: (x[0].duid, x[1].duid)):
           platform.add_platform_command("set_clock_groups -exclusive -group {{{from_}}} -group {{{to}}}", from_=from_, to=to)
        # Make sure add_*_constraint cannot be used again
        del self.clocks
        del self.false_paths

    # Yosys has limited support for real type. It requires that some values be multiplied
    # by 1000 and passed as integers. For details, see:
    # https://github.com/SymbiFlow/symbiflow-arch-defs/blob/master/xc/xc7/techmap/cells_map.v
    def _fix_instance(self, instance):
        if instance.of == "PLLE2_ADV":
            for item in instance.items:
                if isinstance(item, Instance.Parameter) and re.fullmatch("CLKOUT[0-9]_(PHASE|DUTY_CYCLE)", item.name):
                    item.value = wrap(math.floor(_unwrap(item.value) * 1000))

    def build(self, platform, fragment,
        build_dir  = "build",
        build_name = "top",
        run        = True,
        enable_xpm = False,
        **kwargs):

        self._check_properties(platform)

        # Create build directory
        os.makedirs(build_dir, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(build_dir)

        # Finalize design
        if not isinstance(fragment, _Fragment):
            fragment = fragment.get_fragment()
        platform.finalize(fragment)

        # Symbiflow-specific fixes
        for instance in fragment.specials:
            if isinstance(instance, Instance):
                self._fix_instance(instance)

        # Generate timing constraints
        self._build_clock_constraints(platform)

        # Generate verilog
        v_output = platform.get_verilog(fragment, name=build_name, **kwargs)
        named_sc, named_pc = platform.resolve_signals(v_output.ns)
        v_file = build_name + ".v"
        v_output.write(v_file)
        platform.add_source(v_file)

        self._generate_makefile(
            platform   = platform,
            build_name = build_name
        )

        # Generate design constraints
        tools.write_to_file(build_name + ".xdc", _build_xdc(named_sc))
        tools.write_to_file(build_name + ".pcf", _build_pcf(named_sc))
        tools.write_to_file(build_name + ".sdc", _build_sdc(named_pc))

        if run:
            _run_make()

        os.chdir(cwd)

        return v_output.ns

    def add_period_constraint(self, platform, clk, period, phase=0):
        clk.attr.add("keep")
        phase  = math.floor(phase % 360.0 * 1e3)/1e3
        period = math.floor(period*1e3)/1e3 # round to lowest picosecond
        if clk in self.clocks:
            if period != self.clocks[clk][0]:
                raise ValueError("Clock already constrained to {:.2f}ns, new constraint to {:.2f}ns"
                    .format(self.clocks[clk][0], period))
            if phase != self.clocks[clk][1]:
                raise ValueError("Clock already constrained with phase {:.2f}deg, new phase {:.2f}deg"
                    .format(self.clocks[clk][1], phase))
        self.clocks[clk] = (period, phase)

    def add_false_path_constraint(self, platform, from_, to):
        if (from_, to) in self.false_paths or (to, from_) in self.false_paths:
            return
        from_.attr.add("keep")
        to.attr.add("keep")
        self.false_paths.add((from_, to))


def symbiflow_build_args(parser):
    pass


def symbiflow_build_argdict(args):
    return dict()
