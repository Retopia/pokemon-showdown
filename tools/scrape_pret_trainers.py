#!/usr/bin/env python3
"""Scrape trainer parties from pret disassembly repositories.

This script downloads trainer party definitions from a pret project such as
``pokeemerald`` and converts them into a Pokémon Showdown friendly structure.
It relies on the textual layout of files like ``src/data/trainer_parties.h``
which define arrays of ``struct TrainerMon`` values.

Example usage::

    python tools/scrape_pret_trainers.py --trainer ChampionWallace \
        --source pokeemerald

The resulting JSON is suitable for piping into the Showdown team builder or
for additional transformation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import requests

RAW_BASE = "https://raw.githubusercontent.com/pret"
DEFAULT_SOURCES = {
    "pokeemerald": f"{RAW_BASE}/pokeemerald/master/src/data/trainer_parties.h",
    "pokefirered": f"{RAW_BASE}/pokefirered/master/src/data/trainer_parties.h",
    "pokelgfr": f"{RAW_BASE}/pokefirered/master/src/data/trainer_parties.h",
    "pokeblack2": f"{RAW_BASE}/pokeblack2/master/src/data/trainer_parties.h",
    "pokeplatinum": f"{RAW_BASE}/pokeplatinum/master/src/data/trainer_parties.h",
}

STAT_KEYS = ["hp", "atk", "def", "spa", "spd", "spe"]


@dataclass
class TrainerPokemon:
    species: str
    level: int
    item: Optional[str]
    nature: Optional[str]
    moves: List[str]
    ability_slot: Optional[int]
    evs: Optional[Dict[str, int]]
    ivs: Optional[Dict[str, int]]
    raw_fields: Dict[str, str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "species": self.species,
            "level": self.level,
            "item": self.item,
            "nature": self.nature,
            "moves": self.moves,
            "ability_slot": self.ability_slot,
            "evs": self.evs,
            "ivs": self.ivs,
            "raw_fields": self.raw_fields,
        }


def normalize_constant(value: str) -> Optional[str]:
    value = value.strip()
    if not value or value in {"0", "NULL", "NULL", "ITEM_NONE", "MOVE_NONE"}:
        return None
    for prefix in ("SPECIES_", "ITEM_", "MOVE_", "NATURE_"):
        if value.startswith(prefix):
            text = value[len(prefix) :]
            text = text.replace("__", "_")
            text = text.replace("-", "_")
            text = text.replace("'", "")
            text = text.lower().replace("_", " ")
            return " ".join(word.capitalize() for word in text.split())
    if value.startswith("ABILITY_"):
        text = value[len("ABILITY_") :]
        return " ".join(word.capitalize() for word in text.lower().split("_"))
    return value


def parse_stat_array(expr: str) -> Optional[Dict[str, int]]:
    expr = expr.strip().rstrip(",")
    if not expr:
        return None
    if expr.isdigit():
        value = int(expr)
        if value > 31:
            value = 31
        return {stat: value for stat in STAT_KEYS}
    if expr.startswith("{") and expr.endswith("}"):
        values = [v.strip() for v in expr[1:-1].split(",") if v.strip()]
    else:
        func_match = re.search(r"\(([^)]+)\)", expr)
        if not func_match:
            return None
        values = [v.strip() for v in func_match.group(1).split(",")]
    if len(values) != 6:
        return None
    stats: Dict[str, int] = {}
    for key, raw in zip(STAT_KEYS, values):
        try:
            stats[key] = int(raw)
        except ValueError:
            return None
    return stats


def parse_moves(expr: str) -> List[str]:
    expr = expr.strip().rstrip(",")
    if "{" in expr and "}" in expr:
        expr = expr[expr.find("{") + 1 : expr.rfind("}")]
    moves = []
    for token in expr.split(","):
        token = token.strip()
        if not token or token in {"MOVE_NONE", "0"}:
            continue
        move = normalize_constant(token)
        if move:
            moves.append(move)
    return moves


def split_entries(block: str) -> Iterable[str]:
    depth = 0
    start = None
    for idx, ch in enumerate(block):
        if ch == "{" and depth == 0:
            start = idx
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                yield block[start + 1 : idx]
                start = None


def parse_entry(entry_text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for raw_line in entry_text.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or not line.startswith("."):
            continue
        if "=" not in line:
            continue
        key, value = line[1:].split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def extract_party(source_text: str, trainer_symbol: str) -> List[TrainerPokemon]:
    marker = f"sParty_{trainer_symbol}[] = {{"
    try:
        start = source_text.index(marker) + len(marker)
    except ValueError as exc:  # pragma: no cover - user input driven
        raise ValueError(f"Party 'sParty_{trainer_symbol}' not found in source") from exc
    try:
        end = source_text.index("};", start)
    except ValueError as exc:  # pragma: no cover - malformed source
        raise ValueError(f"Party 'sParty_{trainer_symbol}' is not terminated with '}};' in source") from exc
    block = source_text[start:end]
    result: List[TrainerPokemon] = []
    for entry in split_entries(block):
        fields = parse_entry(entry)
        species_raw = fields.get("species")
        if not species_raw:
            continue
        species = normalize_constant(species_raw) or species_raw
        level = int(re.sub(r"[^0-9]", "", fields.get("lvl", "0")) or 0)
        item = normalize_constant(fields.get("heldItem", ""))
        nature = normalize_constant(fields.get("nature", ""))
        ability_slot = fields.get("abilityNum")
        ability_slot_int = int(re.sub(r"[^0-9]", "", ability_slot)) if ability_slot else None
        moves_expr = fields.get("moves", "")
        moves = parse_moves(moves_expr)
        evs = None
        ivs = None
        for key in ("ev", "evs"):
            if key in fields:
                evs = parse_stat_array(fields[key])
        for key in ("iv", "ivs"):
            if key in fields:
                ivs = parse_stat_array(fields[key])
        result.append(
            TrainerPokemon(
                species=species,
                level=level,
                item=item,
                nature=nature,
                moves=moves,
                ability_slot=ability_slot_int,
                evs=evs,
                ivs=ivs,
                raw_fields=fields,
            )
        )
    return result


def format_showdown(pokemon: List[TrainerPokemon]) -> str:
    lines: List[str] = []
    for mon in pokemon:
        header = mon.species
        if mon.item:
            header += f" @ {mon.item}"
        lines.append(header)
        if mon.level and mon.level != 50:
            lines.append(f"Level: {mon.level}")
        if mon.nature:
            lines.append(f"{mon.nature} Nature")
        if mon.evs:
            ev_str = " / ".join(f"{value} {stat.upper()}" for stat, value in mon.evs.items() if value)
            if ev_str:
                lines.append(f"EVs: {ev_str}")
        if mon.ivs and any(value != 31 for value in mon.ivs.values()):
            iv_str = " / ".join(f"{value} {stat.upper()}" for stat, value in mon.ivs.items())
            lines.append(f"IVs: {iv_str}")
        if mon.ability_slot is not None:
            lines.append(f"Ability Slot: {mon.ability_slot}")
        for move in mon.moves:
            lines.append(f"- {move}")
        lines.append("")
    return "\n".join(lines).strip()


def download_source(source: str) -> str:
    if source in DEFAULT_SOURCES:
        url = DEFAULT_SOURCES[source]
    else:
        url = source
    response = requests.get(url, timeout=30, headers={"User-Agent": "ShowdownTrainerScraper/0.1"})
    response.raise_for_status()
    return response.text


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trainer", required=True, help="Trainer party symbol without the sParty_ prefix")
    parser.add_argument(
        "--source",
        default="pokeemerald",
        help="Source key (e.g. pokeemerald) or a direct raw URL to a trainer_parties file",
    )
    parser.add_argument(
        "--format",
        choices=["json", "showdown"],
        default="json",
        help="Output format",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation level")
    args = parser.parse_args(argv)

    source_text = download_source(args.source)
    pokemon = extract_party(source_text, args.trainer)

    if args.format == "json":
        print(json.dumps([mon.to_dict() for mon in pokemon], indent=args.indent))
    else:
        print(format_showdown(pokemon))
    return 0


if __name__ == "__main__":
    sys.exit(main())
