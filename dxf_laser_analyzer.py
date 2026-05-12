#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# DXF Laser Analyzer
# Copyright (C) 2026 Francois RICHARD <fri2@free.fr>
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

"""
DXF Laser Analyzer
==================

Command-line tool to analyze DXF geometry for laser cutting.

It reports:
  1. Total cutting length in mm
  2. Number of drilling / piercing points
     Rule: every closed shape/cycle requires one drilling point.
     Touching closed shapes are counted as separate cycles.
  3. Number of cooling points
     Rule: every coordinate with at least one geometric angle strictly smaller
     than the configured threshold requires one cooling point.
     Default threshold: 120 degrees.

Install:
    pip install ezdxf

Usage:
    python dxf_laser_analyzer.py part.dxf
    python dxf_laser_analyzer.py *.dxf
    python dxf_laser_analyzer.py "C:/laser/jobs/*.dxf"
    python dxf_laser_analyzer.py "C:/laser/jobs/**/*.dxf"

    # Long options remain available:
    python dxf_laser_analyzer.py part.dxf --details
    python dxf_laser_analyzer.py part.dxf --flatten-tolerance 0.02 --endpoint-tolerance 0.03
    python dxf_laser_analyzer.py part.dxf --remove-duplicates
    python dxf_laser_analyzer.py part.dxf --layers CUT,ENGRAVE
    python dxf_laser_analyzer.py part.dxf --exclude-layers CONSTRUCTION,TEXT
    python dxf_laser_analyzer.py part.dxf --json report.json

    # Short aliases are also available:
    python dxf_laser_analyzer.py part.dxf -v
    python dxf_laser_analyzer.py part.dxf -f 0.02 -e 0.03
    python dxf_laser_analyzer.py part.dxf -d
    python dxf_laser_analyzer.py part.dxf -l CUT,ENGRAVE
    python dxf_laser_analyzer.py part.dxf -x CONSTRUCTION,TEXT
    python dxf_laser_analyzer.py part.dxf -j report.json

Notes:
    - DXF geometry is treated as 2D XY geometry.
    - Curves are flattened into short line segments for measurement.
    - Lengths are converted to mm using the DXF $INSUNITS header when possible.
    - If the DXF is unitless, the script assumes the drawing unit is 1 mm.
    - Closed contours made from separate entities are detected by joining endpoints
      within --endpoint-tolerance.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import ezdxf
    from ezdxf import recover
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: ezdxf\n"
        "Install it with: pip install ezdxf"
    ) from exc

try:
    from ezdxf.disassemble import recursive_decompose
except Exception:  # pragma: no cover
    recursive_decompose = None

try:
    from ezdxf import path as ezpath
except Exception:  # pragma: no cover
    ezpath = None


Point = tuple[float, float]


# Common DXF $INSUNITS codes converted to millimetres.
# Unitless DXFs are common in laser workflows, so code 0 is treated as mm.
# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------
# DXF files may declare their drawing unit in the $INSUNITS header variable.
# Laser cutting quotations normally need lengths in millimetres, so every
# coordinate and length is converted to mm before analysis.
#
# Many laser-cutting DXF files are exported as "unitless". In that case, the
# script assumes that 1 drawing unit = 1 mm, which is the most common workflow
# for laser jobs.
INSUNITS_TO_MM: dict[int, tuple[str, float, bool]] = {
    0: ("Unitless / assumed mm", 1.0, True),
    1: ("Inches", 25.4, False),
    2: ("Feet", 304.8, False),
    3: ("Miles", 1_609_344.0, False),
    4: ("Millimetres", 1.0, False),
    5: ("Centimetres", 10.0, False),
    6: ("Metres", 1000.0, False),
    7: ("Kilometres", 1_000_000.0, False),
    8: ("Microinches", 0.0000254, False),
    9: ("Mils", 0.0254, False),
    10: ("Yards", 914.4, False),
    13: ("Microns", 0.001, False),
    14: ("Decimetres", 100.0, False),
}

IGNORED_ENTITY_TYPES = {
    "TEXT",
    "MTEXT",
    "DIMENSION",
    "LEADER",
    "MLEADER",
    "IMAGE",
    "WIPEOUT",
    "VIEWPORT",
    "HATCH",  # Hatches are usually fill/annotation, not laser cut paths.
    "SOLID",
    "3DFACE",
    "TRACE",
}

SUPPORTED_MANUAL_TYPES = {"LINE", "CIRCLE", "ARC", "LWPOLYLINE", "POLYLINE"}


@dataclass(frozen=True)
class Segment:
    start_id: int
    end_id: int
    length_mm: float
    layer: str
    entity_type: str
    handle: str

    def other(self, vertex_id: int) -> int:
        if vertex_id == self.start_id:
            return self.end_id
        if vertex_id == self.end_id:
            return self.start_id
        raise ValueError("vertex is not part of this segment")


@dataclass
class ComponentInfo:
    component_id: int
    closed: bool
    cycle_count: int
    length_mm: float
    vertex_count: int
    segment_count: int


@dataclass
class CoolingPointInfo:
    x_mm: float
    y_mm: float
    angle_deg: float


@dataclass
class AnalysisResult:
    file: str
    units_name: str
    unit_scale_to_mm: float
    unitless_assumed_mm: bool
    total_cutting_length_mm: float
    drilling_points: int
    cooling_points: int
    closed_contours: int
    open_or_ambiguous_contours: int
    ambiguous_junctions: int
    duplicate_segment_candidates: int
    duplicate_segments_removed: int
    ignored_entities: dict[str, int]
    unsupported_entities: dict[str, int]
    analyzed_entities: dict[str, int]
    settings: dict[str, Any]
    components: list[ComponentInfo] | None = None
    cooling_point_details: list[CoolingPointInfo] | None = None


@dataclass
class DxfNoDuplicateWriteResult:
    original_file: str
    output_file: str | None
    written: bool
    duplicate_entities_removed: int
    duplicate_segment_candidates: int


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

class PointSnapper:
    """Merge points that are closer than a tolerance.

    A simple grid is used so that endpoint joining stays fast on large DXF files.
    Coordinates are stored in mm.
    """

    def __init__(self, tolerance_mm: float):
        if tolerance_mm <= 0:
            raise ValueError("endpoint tolerance must be positive")
        self.tol = tolerance_mm
        self._grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.points: list[Point] = []

    def _cell(self, p: Point) -> tuple[int, int]:
        return (math.floor(p[0] / self.tol), math.floor(p[1] / self.tol))

    def snap(self, p: Point) -> int:
        cx, cy = self._cell(p)
        tol2 = self.tol * self.tol

        for gx in range(cx - 1, cx + 2):
            for gy in range(cy - 1, cy + 2):
                for pid in self._grid.get((gx, gy), []):
                    q = self.points[pid]
                    dx = p[0] - q[0]
                    dy = p[1] - q[1]
                    if dx * dx + dy * dy <= tol2:
                        return pid

        pid = len(self.points)
        self.points.append(p)
        self._grid[(cx, cy)].append(pid)
        return pid


def distance(a: Point, b: Point) -> float:
    """Return the 2D Euclidean distance between two points."""

    return math.hypot(b[0] - a[0], b[1] - a[1])


def angle_between(v1: Point, v2: Point) -> float | None:
    """Return the smaller angle between two vectors in degrees."""

    n1 = math.hypot(v1[0], v1[1])
    n2 = math.hypot(v2[0], v2[1])
    if n1 == 0 or n2 == 0:
        return None

    dot = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def unit_info(doc: Any) -> tuple[str, float, bool, int]:
    """Read DXF unit information and return the conversion factor to mm.

    Returns:
        units_name: Human-readable unit name.
        scale_to_mm: Multiplication factor from DXF drawing units to mm.
        unitless_assumed_mm: True when the unit was missing/unknown and mm was assumed.
        code: Raw DXF $INSUNITS code.
    """

    code = int(doc.header.get("$INSUNITS", 0) or 0)
    name, scale, assumed = INSUNITS_TO_MM.get(
        code, (f"Unknown DXF unit code {code} / assumed mm", 1.0, True)
    )
    return name, scale, assumed, code


def layer_allowed(layer: str, include_layers: set[str] | None, exclude_layers: set[str]) -> bool:
    """Return True when an entity layer should be analyzed.

    The comparison is case-insensitive. If include_layers is set, only those
    layers are accepted. exclude_layers always removes matching layers.
    """

    layer_key = layer.lower()
    if include_layers is not None and layer_key not in include_layers:
        return False
    if layer_key in exclude_layers:
        return False
    return True


def clean_points(points: list[Point], min_segment_length: float) -> list[Point]:
    """Remove consecutive points that would create near-zero-length segments.

    This avoids numerical noise from CAD exports or curve flattening from
    creating false graph vertices, false cooling points, or tiny extra lengths.
    """

    if not points:
        return []

    cleaned = [points[0]]
    for p in points[1:]:
        if distance(cleaned[-1], p) >= min_segment_length:
            cleaned.append(p)

    # Preserve closedness while removing a duplicate final point that is too close.
    if len(cleaned) > 2 and distance(cleaned[0], cleaned[-1]) < min_segment_length:
        cleaned[-1] = cleaned[0]

    return cleaned


def append_polyline_segments(
    segments: list[Point],
    new_points: list[Point],
) -> None:
    """Append points without duplicating the joint point."""

    if not new_points:
        return
    if not segments:
        segments.extend(new_points)
        return
    if distance(segments[-1], new_points[0]) < 1e-12:
        segments.extend(new_points[1:])
    else:
        segments.extend(new_points)


def segment_count_for_arc(radius: float, angle_rad: float, flatten_tolerance: float, minimum: int = 8) -> int:
    """Estimate how many line segments are needed to flatten an arc.

    The calculation is based on sagitta error: smaller flatten_tolerance produces
    more segments and therefore a more accurate curve approximation.
    """

    if radius <= 0:
        return minimum
    if flatten_tolerance <= 0:
        return minimum

    angle_rad = abs(angle_rad)
    if angle_rad <= 0:
        return 1

    # Sagitta-based subdivision. Clamp to avoid numerical errors.
    ratio = max(0.0, min(1.0, 1.0 - flatten_tolerance / radius))
    try:
        max_half_angle = math.acos(ratio)
    except ValueError:
        max_half_angle = math.pi / minimum

    if max_half_angle <= 0:
        return max(minimum, 64)

    max_step = 2.0 * max_half_angle
    return max(1, minimum, int(math.ceil(angle_rad / max_step)))


def flatten_arc_by_angles(
    center: Point,
    radius: float,
    start_angle_rad: float,
    total_angle_rad: float,
    flatten_tolerance: float,
) -> list[Point]:
    """Approximate an arc with a list of 2D points.

    The output includes both the start and end point of the arc.
    """

    n = segment_count_for_arc(radius, total_angle_rad, flatten_tolerance)
    points: list[Point] = []
    for i in range(n + 1):
        a = start_angle_rad + total_angle_rad * i / n
        points.append((center[0] + radius * math.cos(a), center[1] + radius * math.sin(a)))
    return points


def flatten_bulge_segment(
    start: Point,
    end: Point,
    bulge: float,
    flatten_tolerance: float,
) -> list[Point]:
    """Flatten one LWPOLYLINE bulge segment.

    In DXF, a bulge value represents an arc between two polyline vertices.
    A zero bulge is a straight segment; a non-zero bulge is converted to an arc
    and then flattened into short line segments.
    """

    if abs(bulge) < 1e-14:
        return [start, end]

    chord = distance(start, end)
    if chord == 0:
        return [start]

    theta = 4.0 * math.atan(bulge)
    radius = abs(chord * (1.0 + bulge * bulge) / (4.0 * bulge))

    ux = (end[0] - start[0]) / chord
    uy = (end[1] - start[1]) / chord
    left_normal = (-uy, ux)
    midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)

    # Signed distance from chord midpoint to arc center.
    h = chord * (1.0 - bulge * bulge) / (4.0 * bulge)
    center = (midpoint[0] + left_normal[0] * h, midpoint[1] + left_normal[1] * h)

    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])

    if bulge > 0:  # counter-clockwise
        if a1 <= a0:
            a1 += 2.0 * math.pi
    else:  # clockwise
        if a1 >= a0:
            a1 -= 2.0 * math.pi

    return flatten_arc_by_angles(center, radius, a0, a1 - a0, flatten_tolerance)


def manual_flatten_entity(entity: Any, flatten_tolerance: float) -> list[list[Point]]:
    """Manual fallback for common laser-cutting entities."""

    etype = entity.dxftype()

    if etype == "LINE":
        s = entity.dxf.start
        e = entity.dxf.end
        return [[(float(s.x), float(s.y)), (float(e.x), float(e.y))]]

    if etype == "CIRCLE":
        c = entity.dxf.center
        r = float(entity.dxf.radius)
        n = segment_count_for_arc(r, 2.0 * math.pi, flatten_tolerance, minimum=32)
        pts = []
        for i in range(n + 1):
            a = 2.0 * math.pi * i / n
            pts.append((float(c.x) + r * math.cos(a), float(c.y) + r * math.sin(a)))
        return [pts]

    if etype == "ARC":
        c = entity.dxf.center
        r = float(entity.dxf.radius)
        start = math.radians(float(entity.dxf.start_angle))
        end = math.radians(float(entity.dxf.end_angle))
        if end <= start:
            end += 2.0 * math.pi
        return [flatten_arc_by_angles((float(c.x), float(c.y)), r, start, end - start, flatten_tolerance)]

    if etype == "LWPOLYLINE":
        raw = list(entity.get_points("xyb"))
        if not raw:
            return []

        closed = bool(entity.closed)
        limit = len(raw) if closed else len(raw) - 1
        pts: list[Point] = []

        for i in range(limit):
            x1, y1, bulge = raw[i]
            x2, y2, _ = raw[(i + 1) % len(raw)]
            part = flatten_bulge_segment(
                (float(x1), float(y1)),
                (float(x2), float(y2)),
                float(bulge),
                flatten_tolerance,
            )
            append_polyline_segments(pts, part)

        return [pts]

    if etype == "POLYLINE":
        vertices = list(entity.vertices)
        pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in vertices]
        if getattr(entity, "is_closed", False) and pts:
            pts.append(pts[0])
        return [pts]

    return []


def flatten_entity(entity: Any, flatten_tolerance: float) -> list[list[Point]]:
    # Convert a DXF entity into one or more chains of 2D points.
    #
    # LINE entities naturally become 2 points.
    # ARC, CIRCLE, SPLINE and curved POLYLINE entities are approximated by many
    # short line segments. This is necessary because length, duplicate detection,
    # cooling points and contour closure are all calculated from segment graphs.
    #
    # The smaller --flatten-tolerance is, the more accurate curves become, but
    # the more segments the script has to process.
    """Flatten one DXF entity into one or more 2D point chains in drawing units."""

    etype = entity.dxftype()

    # First try ezdxf's path engine, which supports more entities and curves.
    if ezpath is not None:
        try:
            make_path = getattr(ezpath, "make_path", None)
            if make_path is not None:
                p = make_path(entity)
                verts = list(p.flattening(distance=flatten_tolerance, segments=8))
                pts = [(float(v.x), float(v.y)) for v in verts]

                # Some closed path outputs do not repeat the first point; enforce closure
                # for known closed entity types.
                if etype in {"CIRCLE"} and len(pts) > 2 and distance(pts[0], pts[-1]) > 1e-9:
                    pts.append(pts[0])
                if etype == "LWPOLYLINE" and bool(entity.closed) and len(pts) > 2 and distance(pts[0], pts[-1]) > 1e-9:
                    pts.append(pts[0])
                if etype == "POLYLINE" and getattr(entity, "is_closed", False) and len(pts) > 2 and distance(pts[0], pts[-1]) > 1e-9:
                    pts.append(pts[0])

                if len(pts) >= 2:
                    return [pts]
        except Exception:
            # Fall through to manual implementation for common entities.
            pass

    return manual_flatten_entity(entity, flatten_tolerance)


def iter_entities(doc: Any) -> Iterator[Any]:
    """Yield analyzable modelspace entities, expanding blocks when possible.

    recursive_decompose() lets the script see geometry nested inside INSERT
    blocks. If decomposition fails, the function falls back to raw modelspace
    entities.
    """

    msp = doc.modelspace()
    if recursive_decompose is not None:
        try:
            yield from recursive_decompose(msp)
            return
        except Exception:
            pass
    yield from msp


def parse_layer_list(value: str | None) -> set[str] | None:
    """Convert a comma-separated layer argument into a lowercase set.

    Returns None when no layer filter was provided.
    """

    if value is None or value.strip() == "":
        return None
    return {item.strip().lower() for item in value.split(",") if item.strip()}


# ---------------------------------------------------------------------------
# Main DXF analysis
# ---------------------------------------------------------------------------
# The analysis workflow is:
#   1. Read the DXF modelspace.
#   2. Decompose supported entities into flattened line segments.
#   3. Snap nearby endpoints together using --endpoint-tolerance.
#   4. Optionally remove duplicate segments before counting.
#   5. Build a graph where vertices are snapped endpoints and edges are cut paths.
#   6. Count drilling points as graph cycles.
#   7. Count cooling points as vertices where at least one angle is below the
#      configured threshold, including multi-segment junctions.
def analyze_dxf(
    dxf_file: Path,
    *,
    flatten_tolerance_mm: float = 0.05,
    endpoint_tolerance_mm: float = 0.05,
    angle_threshold_deg: float = 120.0,
    include_layers: set[str] | None = None,
    exclude_layers: set[str] | None = None,
    include_details: bool = False,
    remove_duplicates: bool = False,
) -> AnalysisResult:
    """Analyze one DXF file and return laser-cutting metrics.

    The function reads the DXF, converts all usable geometry into a snapped
    segment graph, then computes total cutting length, drilling points, cooling
    points, duplicate counts and diagnostic information.
    """

    if exclude_layers is None:
        exclude_layers = set()

    doc, auditor = recover.readfile(str(dxf_file))
    if auditor.has_errors:
        # ezdxf recovered what it could. We continue but the warning appears in settings.
        recover_errors = len(auditor.errors)
    else:
        recover_errors = 0

    units_name, scale_to_mm, unitless_assumed_mm, unit_code = unit_info(doc)
    flatten_tolerance_dxf = flatten_tolerance_mm / scale_to_mm
    min_segment_length_mm = max(endpoint_tolerance_mm * 0.1, 1e-9)

    snapper = PointSnapper(endpoint_tolerance_mm)
    segments: list[Segment] = []
    ignored = Counter()
    unsupported = Counter()
    analyzed = Counter()

    for entity in iter_entities(doc):
        etype = entity.dxftype()
        layer = str(getattr(entity.dxf, "layer", "0") or "0")

        if not layer_allowed(layer, include_layers, exclude_layers):
            ignored[f"{etype} @ skipped layer"] += 1
            continue

        if etype in IGNORED_ENTITY_TYPES:
            ignored[etype] += 1
            continue

        chains = flatten_entity(entity, flatten_tolerance_dxf)
        if not chains:
            unsupported[etype] += 1
            continue

        analyzed[etype] += 1
        handle = str(getattr(entity.dxf, "handle", ""))

        for chain in chains:
            pts_mm = [(x * scale_to_mm, y * scale_to_mm) for x, y in chain]
            pts_mm = clean_points(pts_mm, min_segment_length_mm)
            if len(pts_mm) < 2:
                continue

            for a, b in zip(pts_mm, pts_mm[1:]):
                length_mm = distance(a, b)
                if length_mm < min_segment_length_mm:
                    continue
                start_id = snapper.snap(a)
                end_id = snapper.snap(b)
                if start_id == end_id:
                    continue
                segments.append(
                    Segment(
                        start_id=start_id,
                        end_id=end_id,
                        length_mm=length_mm,
                        layer=layer,
                        entity_type=etype,
                        handle=handle,
                    )
                )

    duplicate_candidates_before_removal = count_duplicate_segment_candidates(segments)
    duplicate_segments_removed = 0
    if remove_duplicates and duplicate_candidates_before_removal:
        original_segment_count = len(segments)
        segments = remove_duplicate_segments(segments)
        duplicate_segments_removed = original_segment_count - len(segments)

    adjacency: dict[int, list[int]] = defaultdict(list)
    for idx, seg in enumerate(segments):
        adjacency[seg.start_id].append(idx)
        adjacency[seg.end_id].append(idx)

    total_length = sum(seg.length_mm for seg in segments)

    components = find_components(segments, adjacency)
    component_infos: list[ComponentInfo] = []
    closed_contours = 0
    open_or_ambiguous = 0

    for cid, edge_ids in enumerate(components, start=1):
        vertex_ids: set[int] = set()
        length_mm = 0.0
        for eid in edge_ids:
            seg = segments[eid]
            vertex_ids.add(seg.start_id)
            vertex_ids.add(seg.end_id)
            length_mm += seg.length_mm

        # Closed-shape counting uses the graph cycle rank:
        #     cycles = edges - vertices + connected_components
        # Here each item returned by find_components() is one connected component,
        # so connected_components = 1.
        #
        # This correctly counts closed shapes that touch at a common point.
        # Example: two circles touching at one point form one connected component,
        # but they still contain two closed cycles and therefore need two piercings.
        cycle_count = max(0, len(edge_ids) - len(vertex_ids) + 1)
        is_closed = cycle_count > 0
        closed_contours += cycle_count

        # A component without any graph cycle is open from a cutting-contour point
        # of view. Junctions are reported separately as ambiguous_junctions.
        if cycle_count == 0:
            open_or_ambiguous += 1

        if include_details:
            component_infos.append(
                ComponentInfo(
                    component_id=cid,
                    closed=is_closed,
                    cycle_count=cycle_count,
                    length_mm=round(length_mm, 6),
                    vertex_count=len(vertex_ids),
                    segment_count=len(edge_ids),
                )
            )

    cooling_details: list[CoolingPointInfo] = []
    cooling_count = 0
    ambiguous_junctions = 0

    for vertex_id, edge_ids in adjacency.items():
        if len(edge_ids) < 2:
            continue

        if len(edge_ids) > 2:
            ambiguous_junctions += 1

        # Count one cooling point per coordinate, even at junctions where more
        # than two segments meet. The reported angle is the smallest angle found
        # between any two incident segments at that coordinate.
        p = snapper.points[vertex_id]
        vectors: list[Point] = []
        for edge_id in edge_ids:
            other_id = segments[edge_id].other(vertex_id)
            q = snapper.points[other_id]
            vectors.append((q[0] - p[0], q[1] - p[1]))

        smallest_angle: float | None = None
        for i in range(len(vectors)):
            for j in range(i + 1, len(vectors)):
                angle = angle_between(vectors[i], vectors[j])
                if angle is None:
                    continue
                if smallest_angle is None or angle < smallest_angle:
                    smallest_angle = angle

        if smallest_angle is not None and smallest_angle < angle_threshold_deg:
            cooling_count += 1
            if include_details:
                cooling_details.append(
                    CoolingPointInfo(
                        x_mm=round(p[0], 6),
                        y_mm=round(p[1], 6),
                        angle_deg=round(smallest_angle, 6),
                    )
                )

    duplicate_candidates = count_duplicate_segment_candidates(segments)

    settings = {
        "flatten_tolerance_mm": flatten_tolerance_mm,
        "endpoint_tolerance_mm": endpoint_tolerance_mm,
        "angle_threshold_deg": angle_threshold_deg,
        "remove_duplicate_geometry": remove_duplicates,
        "duplicate_segment_candidates_before_removal": duplicate_candidates_before_removal,
        "duplicate_segments_removed": duplicate_segments_removed,
        "included_layers": sorted(include_layers) if include_layers is not None else "all",
        "excluded_layers": sorted(exclude_layers),
        "dxf_insunits_code": unit_code,
        "recover_errors": recover_errors,
        "segments_after_flattening": len(segments),
    }

    return AnalysisResult(
        file=str(dxf_file),
        units_name=units_name,
        unit_scale_to_mm=scale_to_mm,
        unitless_assumed_mm=unitless_assumed_mm,
        total_cutting_length_mm=round(total_length, 6),
        drilling_points=closed_contours,
        cooling_points=cooling_count,
        closed_contours=closed_contours,
        open_or_ambiguous_contours=open_or_ambiguous,
        ambiguous_junctions=ambiguous_junctions,
        duplicate_segment_candidates=duplicate_candidates,
        duplicate_segments_removed=duplicate_segments_removed,
        ignored_entities=dict(sorted(ignored.items())),
        unsupported_entities=dict(sorted(unsupported.items())),
        analyzed_entities=dict(sorted(analyzed.items())),
        settings=settings,
        components=component_infos if include_details else None,
        cooling_point_details=cooling_details if include_details else None,
    )


def find_components(segments: list[Segment], adjacency: dict[int, list[int]]) -> list[list[int]]:
    """Group connected graph edges into connected components.

    Each returned component is a list of segment indexes. Components are later
    used to compute graph cycle counts, which correspond to closed shapes.
    """

    components: list[list[int]] = []
    visited_edges: set[int] = set()

    for start_edge in range(len(segments)):
        if start_edge in visited_edges:
            continue

        comp: list[int] = []
        queue: deque[int] = deque([start_edge])
        visited_edges.add(start_edge)

        while queue:
            eid = queue.popleft()
            comp.append(eid)
            seg = segments[eid]
            for vertex_id in (seg.start_id, seg.end_id):
                for next_edge in adjacency[vertex_id]:
                    if next_edge not in visited_edges:
                        visited_edges.add(next_edge)
                        queue.append(next_edge)

        components.append(comp)

    return components


def count_duplicate_segment_candidates(segments: list[Segment]) -> int:
    """Count exact duplicate snapped segments.

    This does not detect partially overlapping collinear segments; it only detects
    entities that share the same snapped endpoints.
    """

    keys = Counter()
    for seg in segments:
        a, b = sorted((seg.start_id, seg.end_id))
        keys[(a, b)] += 1
    return sum(count - 1 for count in keys.values() if count > 1)


# ---------------------------------------------------------------------------
# Duplicate detection for analysis
# ---------------------------------------------------------------------------
# Duplicate geometry is detected after flattening and endpoint snapping.
# This makes the method work not only for duplicated LINE entities, but also for
# duplicated arcs, circles, splines and polylines, because they all become the
# same kind of small segment list before comparison.
def remove_duplicate_segments(segments: list[Segment]) -> list[Segment]:
    """Remove duplicate snapped segments while preserving the first occurrence.

    Two segments are considered duplicates when they connect the same snapped
    endpoints, regardless of direction. Because curves, arcs and circles are
    flattened before this step, duplicate curved geometry is removed as duplicate
    small segments.
    """

    unique_segments: list[Segment] = []
    seen: set[tuple[int, int]] = set()

    for seg in segments:
        key = tuple(sorted((seg.start_id, seg.end_id)))
        if key in seen:
            continue
        seen.add(key)
        unique_segments.append(seg)

    return unique_segments


def flattened_entity_segment_keys(
    entity: Any,
    *,
    snapper: PointSnapper,
    scale_to_mm: float,
    flatten_tolerance_dxf: float,
    min_segment_length_mm: float,
) -> list[tuple[int, int]]:
    """Return direction-independent snapped segment keys for one entity.

    Curved geometry is flattened first, so duplicate arcs, circles, splines,
    curves and polylines can be compared with the same representation as lines.
    """

    keys: list[tuple[int, int]] = []
    chains = flatten_entity(entity, flatten_tolerance_dxf)

    for chain in chains:
        pts_mm = [(x * scale_to_mm, y * scale_to_mm) for x, y in chain]
        pts_mm = clean_points(pts_mm, min_segment_length_mm)
        if len(pts_mm) < 2:
            continue

        for a, b in zip(pts_mm, pts_mm[1:]):
            if distance(a, b) < min_segment_length_mm:
                continue
            start_id = snapper.snap(a)
            end_id = snapper.snap(b)
            if start_id == end_id:
                continue
            keys.append(tuple(sorted((start_id, end_id))))

    return keys


# ---------------------------------------------------------------------------
# Optional DXF writer: create file_ND.dxf
# ---------------------------------------------------------------------------
# This writer removes duplicate whole entities from a copy of the original DXF.
# It is intentionally conservative:
#   - if an entire entity is duplicated, the duplicate entity is removed;
#   - if an entity only partly overlaps another one, it is kept.
# This avoids accidentally damaging valid geometry.
def write_dxf_without_duplicate_entities(
    dxf_file: Path,
    *,
    flatten_tolerance_mm: float = 0.05,
    endpoint_tolerance_mm: float = 0.05,
    include_layers: set[str] | None = None,
    exclude_layers: set[str] | None = None,
) -> DxfNoDuplicateWriteResult:
    """Create file_ND.dxf with duplicate top-level entities removed.

    The original file is never modified. A copy is written only when at least one
    duplicate removable entity is found.

    Entities are removed only when their complete flattened geometry is already
    represented by previously seen geometry. Partially overlapping entities are
    kept to avoid deleting intentional geometry.
    """

    if exclude_layers is None:
        exclude_layers = set()

    doc, auditor = recover.readfile(str(dxf_file))
    if auditor.has_errors:
        # Continue with the recovered document, as analyze_dxf() does.
        pass

    _units_name, scale_to_mm, _unitless_assumed_mm, _unit_code = unit_info(doc)
    flatten_tolerance_dxf = flatten_tolerance_mm / scale_to_mm
    min_segment_length_mm = max(endpoint_tolerance_mm * 0.1, 1e-9)
    snapper = PointSnapper(endpoint_tolerance_mm)

    seen_segments: set[tuple[int, int]] = set()
    entities_to_delete: list[Any] = []
    duplicate_segment_candidates = 0

    msp = doc.modelspace()
    for entity in list(msp):
        etype = entity.dxftype()
        layer = str(getattr(entity.dxf, "layer", "0") or "0")

        if not layer_allowed(layer, include_layers, exclude_layers):
            continue
        if etype in IGNORED_ENTITY_TYPES:
            continue

        keys = flattened_entity_segment_keys(
            entity,
            snapper=snapper,
            scale_to_mm=scale_to_mm,
            flatten_tolerance_dxf=flatten_tolerance_dxf,
            min_segment_length_mm=min_segment_length_mm,
        )
        if not keys:
            continue

        repeated_keys = sum(1 for key in keys if key in seen_segments)
        duplicate_segment_candidates += repeated_keys

        if repeated_keys == len(keys):
            entities_to_delete.append(entity)
            continue

        seen_segments.update(keys)

    if not entities_to_delete:
        return DxfNoDuplicateWriteResult(
            original_file=str(dxf_file),
            output_file=None,
            written=False,
            duplicate_entities_removed=0,
            duplicate_segment_candidates=duplicate_segment_candidates,
        )

    for entity in entities_to_delete:
        msp.delete_entity(entity)

    output_file = dxf_file.with_name(f"{dxf_file.stem}_ND.dxf")
    doc.saveas(str(output_file))

    return DxfNoDuplicateWriteResult(
        original_file=str(dxf_file),
        output_file=str(output_file),
        written=True,
        duplicate_entities_removed=len(entities_to_delete),
        duplicate_segment_candidates=duplicate_segment_candidates,
    )


def result_to_dict(result: AnalysisResult) -> dict[str, Any]:
    """Convert an AnalysisResult dataclass to a JSON-friendly dictionary."""

    data = asdict(result)
    # Avoid noisy null fields in default mode.
    if data.get("components") is None:
        data.pop("components", None)
    if data.get("cooling_point_details") is None:
        data.pop("cooling_point_details", None)
    return data


def print_human_report(result: AnalysisResult) -> None:
    """Print the detailed single-file analysis report."""

    print("DXF LASER ANALYSIS")
    print("==================")
    print(f"File: {result.file}")
    print(f"Units: {result.units_name}")
    if result.unitless_assumed_mm:
        print("Warning: DXF is unitless or has unknown units; drawing units were assumed to be mm.")
    print()
    print(f"Total cutting length: {result.total_cutting_length_mm:.3f} mm")
    print(f"Drilling / piercing points: {result.drilling_points}")
    print(f"Cooling points: {result.cooling_points}")
    print()
    print(f"Closed contours: {result.closed_contours}")
    print(f"Open or ambiguous contours: {result.open_or_ambiguous_contours}")
    print(f"Ambiguous junctions: {result.ambiguous_junctions}")
    print(f"Duplicate segment candidates: {result.duplicate_segment_candidates}")
    print(f"Duplicate segments removed: {result.duplicate_segments_removed}")
    print()

    if result.analyzed_entities:
        print("Analyzed entities:")
        for key, value in result.analyzed_entities.items():
            print(f"  {key}: {value}")
        print()

    if result.ignored_entities:
        print("Ignored entities:")
        for key, value in result.ignored_entities.items():
            print(f"  {key}: {value}")
        print()

    if result.unsupported_entities:
        print("Unsupported entities:")
        for key, value in result.unsupported_entities.items():
            print(f"  {key}: {value}")
        print()

    print("Settings:")
    for key, value in result.settings.items():
        print(f"  {key}: {value}")

    if result.components is not None:
        print()
        print("Components:")
        for comp in result.components:
            status = "closed" if comp.closed else "open/ambiguous"
            print(
                f"  #{comp.component_id}: {status}, "
                f"cycles={comp.cycle_count}, "
                f"length={comp.length_mm:.3f} mm, "
                f"vertices={comp.vertex_count}, segments={comp.segment_count}"
            )

    if result.cooling_point_details is not None:
        print()
        print("Cooling point details:")
        for point in result.cooling_point_details:
            print(f"  x={point.x_mm:.3f} mm, y={point.y_mm:.3f} mm, angle={point.angle_deg:.3f}°")


def build_arg_parser() -> argparse.ArgumentParser:
    """Create and configure the command-line argument parser."""

    parser = argparse.ArgumentParser(
        description="Analyze DXF files for laser cutting length, drilling points, cooling points, and duplicates.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Short and long option forms:\n"
            "  -f, --flatten-tolerance        Curve flattening tolerance in mm\n"
            "  -e, --endpoint-tolerance       Endpoint joining tolerance in mm\n"
            "  -a, --angle-threshold          Cooling angle threshold in degrees\n"
            "  -d, --remove-duplicates        Remove duplicate geometry before analysis\n"
            "  -n, -w, --write-dxf-noduplicate\n"
            "                                   Write file_ND.dxf copy when duplicates are found\n"
            "  -l, --layers                   Include only these comma-separated layers\n"
            "  -x, --exclude-layers           Exclude these comma-separated layers\n"
            "  -v, --details                  Print detailed contour and cooling-point data\n"
            "  -j, --json                     Write report to JSON file\n"
            "      --json-only                Print only JSON to stdout\n"
            "\n"
            "Examples:\n"
            "  python dxf_laser_analyzer.py *.dxf --remove-duplicates\n"
            "  python dxf_laser_analyzer.py *.dxf -d\n"
            "  python dxf_laser_analyzer.py *.dxf --write-dxf-noduplicate\n"
            "  python dxf_laser_analyzer.py *.dxf -n\n"
            "  python dxf_laser_analyzer.py *.dxf -w\n"
            "  python dxf_laser_analyzer.py part.dxf --details --json report.json\n"
            "  python dxf_laser_analyzer.py part.dxf -v -j report.json"
        ),
    )

    parser.add_argument(
        "dxf_files",
        type=str,
        nargs="+",
        help="Path(s), wildcard pattern(s), or both for one or more DXF files to analyze",
    )
    parser.add_argument(
        "-f",
        "--flatten-tolerance",
        type=float,
        default=0.05,
        help="Curve flattening tolerance in mm. Smaller is more accurate but slower. Default: 0.05",
    )
    parser.add_argument(
        "-e",
        "--endpoint-tolerance",
        type=float,
        default=0.05,
        help="Endpoint joining tolerance in mm for contour detection. Default: 0.05",
    )
    parser.add_argument(
        "-a",
        "--angle-threshold",
        type=float,
        default=120.0,
        help="Cooling point threshold in degrees. Angles below this value are counted. Default: 120",
    )
    parser.add_argument(
        "-d",
        "--remove-duplicates",
        action="store_true",
        help=(
            "Remove duplicate snapped segments before analysis. This prevents "
            "stacked duplicate lines, arcs, curves, circles, or forms from "
            "inflating length or contour counts."
        ),
    )
    parser.add_argument(
        "-n",
        "-w",
        "--write-dxf-noduplicate",
        action="store_true",
        help=(
            "Create a duplicate-free copy named file_ND.dxf in the same directory, "
            "only when removable duplicate entities are found. The original DXF is not modified."
        ),
    )
    parser.add_argument(
        "-l",
        "--layers",
        type=str,
        default=None,
        help="Comma-separated list of layers to include. Default: all layers",
    )
    parser.add_argument(
        "-x",
        "--exclude-layers",
        type=str,
        default=None,
        help="Comma-separated list of layers to exclude",
    )
    parser.add_argument(
        "-v",
        "--details",
        action="store_true",
        help="Include component and cooling point coordinate details",
    )
    parser.add_argument(
        "-j",
        "--json",
        type=Path,
        default=None,
        help="Write JSON report to this file",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print only JSON to stdout",
    )

    return parser


def print_multi_file_table(results: list[AnalysisResult]) -> None:
    """Print a compact one-row-per-file table for batch analysis."""

    rows = []
    for result in results:
        duplicate_count = (
            result.duplicate_segments_removed
            if result.duplicate_segments_removed > 0
            else result.duplicate_segment_candidates
        )
        rows.append(
            (
                Path(result.file).name,
                str(result.drilling_points),
                str(result.cooling_points),
                str(duplicate_count),
                f"{result.total_cutting_length_mm:.3f}",
            )
        )

    headers = ("name", "drill", "cool", "duplicate", "length")
    widths = [len(header) for header in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    def fmt(row: tuple[str, str, str, str, str]) -> str:
        return " | ".join(value.ljust(widths[i]) for i, value in enumerate(row))

    print(fmt(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(fmt(row))


def expand_file_arguments(file_args: list[str]) -> list[Path]:
    """Expand command-line file arguments, including wildcard patterns.

    This is especially useful on Windows, where wildcards such as *.dxf are not
    always expanded by the shell before Python receives the arguments.
    """

    expanded: list[Path] = []
    seen: set[str] = set()
    unmatched_patterns: list[str] = []

    for item in file_args:
        if glob.has_magic(item):
            matches = sorted(glob.glob(item, recursive=True))
            if not matches:
                unmatched_patterns.append(item)
                continue
        else:
            matches = [item]

        for match in matches:
            path = Path(match)
            key = str(path.resolve()) if path.exists() else str(path)
            if key not in seen:
                expanded.append(path)
                seen.add(key)

    if unmatched_patterns:
        raise ValueError("No files matched wildcard pattern(s): " + ", ".join(unmatched_patterns))

    return expanded


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Program entry point used by the command-line interface.

    Returns a process exit code: 0 on success, 2 on analysis or write errors.
    """

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Expand explicit file paths and wildcard patterns such as *.dxf.
    # This is done inside Python so the script works consistently on Windows,
    # PowerShell, cmd.exe, Linux and macOS shells.
    try:
        dxf_files = expand_file_arguments(args.dxf_files)
    except ValueError as exc:
        parser.error(str(exc))

    if not dxf_files:
        parser.error("No DXF files were provided or matched.")

    missing_files = [path for path in dxf_files if not path.exists()]
    if missing_files:
        parser.error("DXF file does not exist: " + ", ".join(str(path) for path in missing_files))

    include_layers = parse_layer_list(args.layers)
    exclude_layers = parse_layer_list(args.exclude_layers) or set()

    # Optional write pass: create *_ND.dxf copies only for files where complete
    # duplicate entities were found. This does not modify the original DXF files.
    write_results: list[DxfNoDuplicateWriteResult] = []
    if args.write_dxf_noduplicate:
        for dxf_file in dxf_files:
            try:
                write_results.append(
                    write_dxf_without_duplicate_entities(
                        dxf_file,
                        flatten_tolerance_mm=args.flatten_tolerance,
                        endpoint_tolerance_mm=args.endpoint_tolerance,
                        include_layers=include_layers,
                        exclude_layers=exclude_layers,
                    )
                )
            except Exception as exc:
                print(f"Error while writing no-duplicate DXF for '{dxf_file}': {exc}", file=sys.stderr)
                return 2

    # Analysis pass: this always runs, even when -n/-w is used, so the user gets
    # the normal length / drill / cool / duplicate report after optional writing.
    results: list[AnalysisResult] = []
    for dxf_file in dxf_files:
        try:
            results.append(
                analyze_dxf(
                    dxf_file,
                    flatten_tolerance_mm=args.flatten_tolerance,
                    endpoint_tolerance_mm=args.endpoint_tolerance,
                    angle_threshold_deg=args.angle_threshold,
                    include_layers=include_layers,
                    exclude_layers=exclude_layers,
                    include_details=args.details,
                    remove_duplicates=args.remove_duplicates,
                )
            )
        except Exception as exc:
            print(f"Error while analyzing DXF '{dxf_file}': {exc}", file=sys.stderr)
            return 2

    data: dict[str, Any] | list[dict[str, Any]]
    data = result_to_dict(results[0]) if len(results) == 1 else [result_to_dict(result) for result in results]

    if args.json is not None:
        args.json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.json_only:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif len(results) == 1:
        print_human_report(results[0])
        if args.json is not None:
            print()
            print(f"JSON report written to: {args.json}")
    else:
        print_multi_file_table(results)
        if args.json is not None:
            print()
            print(f"JSON report written to: {args.json}")

    if write_results and not args.json_only:
        print()
        print("No-duplicate DXF output:")
        for write_result in write_results:
            if write_result.written:
                print(
                    f"  {Path(write_result.original_file).name}: wrote "
                    f"{Path(write_result.output_file or '').name} "
                    f"({write_result.duplicate_entities_removed} duplicate entities removed)"
                )
            else:
                print(f"  {Path(write_result.original_file).name}: no duplicate entity found, no file written")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
