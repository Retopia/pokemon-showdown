#!/usr/bin/env python3
"""Scrape trainer parties from pret disassembly repositories.

The script can export a single trainer party (matching the original behaviour)
*or* walk every gym leader, Elite Four member, and champion exposed by the
configured pret repositories.  The batch mode writes two JSON files per game:
one containing just the trainer names and another with the full Showdown-ready
team data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import requests

RAW_BASE = "https://raw.githubusercontent.com/pret"
STAT_KEYS = ["hp", "atk", "def", "spa", "spd", "spe"]


@dataclass
class PretGame:
    """Descriptor for a pret repository that exposes trainer data in C headers."""

    key: str
    repo: str
    trainers_path: str = "src/data/trainers.h"
    parties_path: str = "src/data/trainer_parties.h"

    @property
    def trainers_url(self) -> str:
        return f"{RAW_BASE}/{self.repo}/master/{self.trainers_path}"

    @property
    def parties_url(self) -> str:
        return f"{RAW_BASE}/{self.repo}/master/{self.parties_path}"


# Only GBA-era games use the same layout; additional repos can be added later.
PRET_GAMES: Dict[str, PretGame] = {
    "rse": PretGame(key="rse", repo="pokeemerald"),
    "frlg": PretGame(key="frlg", repo="pokefirered"),
}

DEFAULT_SOURCES: Dict[str, str] = {
    "pokeemerald": PRET_GAMES["rse"].parties_url,
    "pokefirered": PRET_GAMES["frlg"].parties_url,
    "pokelgfr": PRET_GAMES["frlg"].parties_url,
    "rse": PRET_GAMES["rse"].parties_url,
    "frlg": PRET_GAMES["frlg"].parties_url,
}


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


@dataclass
class TrainerMetadata:
    symbol: str
    name: str
    trainer_class: str
    party_symbol: Optional[str]


def normalize_constant(value: str) -> Optional[str]:
    value = value.strip()
    if not value or value in {"0", "NULL", "ITEM_NONE", "MOVE_NONE"}:
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
    except ValueError as exc:
        raise ValueError(f"Party 'sParty_{trainer_symbol}' not found in source") from exc
    try:
        end = source_text.index("};", start)
    except ValueError as exc:
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


def iter_trainer_entries(source_text: str) -> Iterator[Tuple[str, str]]:
    pattern = re.compile(r"\[([A-Z0-9_]+)\]\s*=\s*\{", re.MULTILINE)
    pos = 0
    while True:
        match = pattern.search(source_text, pos)
        if not match:
            break
        symbol = match.group(1)
        brace_depth = 1
        idx = match.end()
        while idx < len(source_text) and brace_depth:
            if source_text[idx] == "{":
                brace_depth += 1
            elif source_text[idx] == "}":
                brace_depth -= 1
            idx += 1
        entry_text = source_text[match.end() : idx - 1]
        pos = idx
        yield symbol, entry_text


def parse_trainer_metadata(source_text: str) -> List[TrainerMetadata]:
    metadata: List[TrainerMetadata] = []
    for symbol, entry_text in iter_trainer_entries(source_text):
        trainer_class_match = re.search(r"\.trainerClass\s*=\s*([A-Z0-9_]+)", entry_text)
        name_match = re.search(r"\.trainerName\s*=\s*_\(([^)]+)\)", entry_text)
        party_match = re.search(r"sParty_([A-Za-z0-9_]+)", entry_text)
        trainer_class = trainer_class_match.group(1) if trainer_class_match else ""
        raw_name = name_match.group(1) if name_match else '""'
        cleaned_name = raw_name.strip().strip('"')
        name = cleaned_name.title()
        metadata.append(
            TrainerMetadata(
                symbol=symbol,
                name=name,
                trainer_class=trainer_class,
                party_symbol=party_match.group(1) if party_match else None,
            )
        )
    return metadata


def categorize_trainer(trainer_class: str) -> Optional[str]:
    if "GYM_LEADER" in trainer_class:
        return "gym_leader"
    if "ELITE_FOUR" in trainer_class:
        return "elite_four"
    if "CHAMPION" in trainer_class or "CHAMP" in trainer_class:
        return "champion"
    return None


def sanitize_filename(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", text.lower())


def download_source(identifier: str) -> str:
    url = DEFAULT_SOURCES.get(identifier, identifier)
    response = requests.get(url, timeout=30, headers={"User-Agent": "ShowdownTrainerScraper/0.2"})
    response.raise_for_status()
    return response.text


def run_full_scrape(keys: Sequence[str], output_dir: str, indent: int) -> None:
    os.makedirs(output_dir, exist_ok=True)
    for key in keys:
        if key not in PRET_GAMES:
            raise ValueError(f"Unknown game '{key}'. Available: {', '.join(sorted(PRET_GAMES))}")
        spec = PRET_GAMES[key]
        trainers_src = download_source(spec.trainers_url)
        parties_src = download_source(spec.parties_url)

        metadata = [entry for entry in parse_trainer_metadata(trainers_src) if categorize_trainer(entry.trainer_class)]
        summary: Dict[str, List[str]] = {"gym_leader": [], "elite_four": [], "champion": []}
        detailed: List[Dict[str, object]] = []

        for entry in metadata:
            category = categorize_trainer(entry.trainer_class)
            if not category or not entry.party_symbol:
                continue
            try:
                party = extract_party(parties_src, entry.party_symbol)
            except ValueError:
                continue
            summary[category].append(entry.name)
            detailed.append(
                {
                    "trainer": entry.name,
                    "category": category,
                    "trainer_symbol": entry.symbol,
                    "party_symbol": entry.party_symbol,
                    "pokemon": [mon.to_dict() for mon in party],
                }
            )

        base = sanitize_filename(key)
        summary_path = os.path.join(output_dir, f"{base}-trainers.json")
        teams_path = os.path.join(output_dir, f"{base}-teams.json")
        with open(summary_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=indent)
        with open(teams_path, "w", encoding="utf-8") as handle:
            json.dump(detailed, handle, indent=indent)


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trainer", help="Trainer party symbol without the sParty_ prefix")
    parser.add_argument(
        "--source",
        default="pokeemerald",
        help="Source key (e.g. pokeemerald) or a direct raw URL to a trainer_parties file",
    )
    parser.add_argument(
        "--format",
        choices=["json", "showdown"],
        default="json",
        help="Output format for single trainer mode",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation level")
    parser.add_argument("--full", action="store_true", help="Scrape every configured game")
    parser.add_argument("--games", nargs="*", help="Subset of game keys to process in full scrape mode")
    parser.add_argument("--output-dir", default="trainer-dumps", help="Directory for full scrape output")
    args = parser.parse_args(argv)

    if args.full or args.games:
        game_keys = args.games if args.games else sorted(PRET_GAMES.keys())
        run_full_scrape(game_keys, args.output_dir, args.indent)
        return 0

    if not args.trainer:
        parser.error("--trainer is required when not running in --full mode")

    source_text = download_source(args.source)
    pokemon = extract_party(source_text, args.trainer)

    if args.format == "json":
        print(json.dumps([mon.to_dict() for mon in pokemon], indent=args.indent))
    else:
        print(format_showdown(pokemon))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
