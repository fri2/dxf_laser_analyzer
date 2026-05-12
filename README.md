# DXF Laser Analyzer

**Author:** Francois RICHARD <fri2@free.fr>  
**License:** GPL-3.0-only

DXF Laser Analyzer is a command-line Python script for analyzing DXF files intended for laser cutting.

It reports:

1. **Total cutting length in mm**
2. **Number of drilling / piercing points**
3. **Number of cooling points**
4. **Duplicate geometry count**

It can also remove duplicated geometry from the analysis, or create a cleaned DXF copy without duplicate entities.

---

## Features

- Analyze one or several DXF files.
- Support wildcard file arguments such as `*.dxf`.
- Convert supported DXF geometry to millimetres.
- Flatten curves into short line segments for measurement.
- Detect closed contours and count drilling / piercing points.
- Detect sharp angles and count cooling points.
- Detect duplicated geometry.
- Optionally ignore duplicated geometry during analysis.
- Optionally create a cleaned DXF copy named `file_ND.dxf`.
- Print either a detailed single-file report or a compact multi-file table.
- Export JSON reports.

---

## Installation

The script requires Python and the `ezdxf` package.

```bash
pip install ezdxf
```

---

## Basic usage

Analyze one file:

```bash
python dxf_laser_analyzer.py part.dxf
```

Analyze all DXF files in the current directory:

```bash
python dxf_laser_analyzer.py *.dxf
```

Analyze DXF files using a wildcard path:

```bash
python dxf_laser_analyzer.py "C:/laser/jobs/*.dxf"
```

Analyze DXF files recursively:

```bash
python dxf_laser_analyzer.py "C:/laser/jobs/**/*.dxf"
```

---

## Output for multiple files

When several DXF files are analyzed, the script prints a compact table with one row per file:

```text
name      | drill | cool | duplicate | length
----------+-------+------+-----------+---------
part1.dxf | 23    | 49   | 4         | 1542.381
part2.dxf | 12    | 18   | 0         | 822.500
```

Columns:

- `name`: DXF file name
- `drill`: drilling / piercing point count
- `cool`: cooling point count
- `duplicate`: duplicate geometry count
- `length`: total cutting length in mm

---

## Command-line options

### Long options

```bash
python dxf_laser_analyzer.py part.dxf --details
python dxf_laser_analyzer.py part.dxf --flatten-tolerance 0.02
python dxf_laser_analyzer.py part.dxf --endpoint-tolerance 0.03
python dxf_laser_analyzer.py part.dxf --angle-threshold 120
python dxf_laser_analyzer.py part.dxf --remove-duplicates
python dxf_laser_analyzer.py part.dxf --write-dxf-noduplicate
python dxf_laser_analyzer.py part.dxf --layers CUT,ENGRAVE
python dxf_laser_analyzer.py part.dxf --exclude-layers CONSTRUCTION,TEXT
python dxf_laser_analyzer.py part.dxf --json report.json
python dxf_laser_analyzer.py part.dxf --json-only
```

### Short aliases

```text
-f  --flatten-tolerance
-e  --endpoint-tolerance
-a  --angle-threshold
-d  --remove-duplicates
-n  --write-dxf-noduplicate
-w  --write-dxf-noduplicate
-l  --layers
-x  --exclude-layers
-v  --details
-j  --json
```

Examples:

```bash
python dxf_laser_analyzer.py *.dxf -d
python dxf_laser_analyzer.py *.dxf -n
python dxf_laser_analyzer.py *.dxf -w
python dxf_laser_analyzer.py part.dxf -v -j report.json
```

---

## Geometry handling

The script treats DXF geometry as 2D XY geometry.

Supported or commonly handled entities include:

- `LINE`
- `ARC`
- `CIRCLE`
- `LWPOLYLINE`
- `POLYLINE`
- spline-like curves when they can be flattened by `ezdxf`

Ignored entities include typical annotation or non-cutting entities such as:

- `TEXT`
- `MTEXT`
- `DIMENSION`
- `HATCH`
- `IMAGE`
- `VIEWPORT`

Curves are flattened into short line segments before measurement and graph analysis.

The flattening precision is controlled by:

```bash
--flatten-tolerance
```

or:

```bash
-f
```

A smaller value gives a more accurate curve approximation but increases processing time.

---

## Units

The script converts DXF drawing units to millimetres using the DXF `$INSUNITS` header when possible.

If the DXF file is unitless or uses an unknown unit code, the script assumes:

```text
1 drawing unit = 1 mm
```

This is common in laser-cutting workflows.

---

## How duplicates are treated

### In short

- Duplicate `LINE`, `ARC`, `CIRCLE`, `POLYLINE`, `LWPOLYLINE`, and spline-like geometry can be detected because everything is compared as flattened segments.
- Reversed geometry is still considered duplicate.
- Tiny coordinate differences can be absorbed by `--endpoint-tolerance / -e`.
- `--remove-duplicates / -d` affects only the calculated analysis.
- `--write-dxf-noduplicate / -n / -w` writes a cleaned copy, but only removes complete duplicate entities, not partial overlaps.

### Detailed explanation

The script detects duplicates after converting the DXF geometry into a common internal representation.

DXF files can contain many different entity types:

- simple `LINE` entities
- `ARC` entities
- `CIRCLE` entities
- `LWPOLYLINE` or `POLYLINE` entities
- spline-like curves

Comparing these entities directly is difficult because two visually identical shapes may be stored in different ways by different CAD/CAM programs.

For example, a circle may be exported as a `CIRCLE` entity in one file, but as a closed polyline made of many short segments in another file.

To make duplicate detection more reliable, the script first flattens supported geometry into short line segments.

A straight line remains one segment, while an arc, circle, curved polyline, or spline-like curve is approximated by several short segments.

The precision of this approximation is controlled by:

```bash
--flatten-tolerance
```

or:

```bash
-f
```

A smaller tolerance gives a more accurate comparison for curves, but it also creates more segments and therefore takes more time.

After flattening, all coordinates are converted to millimetres and endpoints are snapped together using:

```bash
--endpoint-tolerance
```

or:

```bash
-e
```

This snapping step is important because CAD exports often contain very small numerical differences.

For example, one endpoint may be at:

```text
X = 10.000000
```

while the duplicate endpoint may be at:

```text
X = 10.000003
```

Without endpoint snapping, those two endpoints would be treated as different points even though, for laser cutting, they represent the same physical location.

A duplicate segment is considered identical when it connects the same two snapped endpoints, regardless of direction.

This means that a segment from A to B and another segment from B to A are treated as duplicates.

This is useful because some CAD exports reverse the drawing direction of duplicated geometry, but the laser would still cut the same path twice.

The duplicate count shown in the report is based on these duplicate snapped segments.

It is therefore a practical laser-cutting duplicate count, not a strict DXF-object comparison.

The goal is to detect geometry that would cause the laser to cut the same path more than once.

### Duplicate-removal modes

There are two duplicate-removal modes.

#### 1. Analysis-only duplicate removal

```bash
--remove-duplicates
```

or:

```bash
-d
```

This removes duplicate segments only from the internal analysis.

The original DXF file is not modified and no new DXF file is written.

This option is useful when you only want correct length, drilling and cooling results without changing your source files.

With this option, duplicate geometry does not inflate:

- total cutting length
- drilling point count
- cooling point count
- graph ambiguity

#### 2. DXF copy generation without duplicate entities

```bash
--write-dxf-noduplicate
```

or:

```bash
-n
```

or:

```bash
-w
```

This creates a new DXF file named:

```text
file_ND.dxf
```

in the same directory as the original file.

The original file is never modified.

A new file is written only if removable duplicate entities are found.

If no duplicate entity is found, no `_ND.dxf` file is created.

This writing mode is deliberately conservative.

It removes only complete duplicate entities, meaning that the whole flattened representation of an entity must already be present in previously seen geometry.

If an entity only partially overlaps another entity, it is kept.

This avoids accidentally deleting geometry that may be intentional, such as:

- shared borders
- tabs
- construction details
- partially overlapping design features

---

## How drilling / piercing points are treated

### In short

- One closed graph cycle is counted as one drilling point.
- Closed shapes made from separate `LINE`, `ARC`, or `POLYLINE` entities are counted correctly when their endpoints meet within `--endpoint-tolerance`.
- Touching closed shapes can still produce several drilling points.
- Open contours do not create drilling points.
- Tiny gaps can prevent a drilling point from being counted.
- Duplicate paths should be removed from analysis with `-d` when they inflate the graph.

### Detailed explanation

A drilling point, also called a piercing point, represents the point where the laser has to pierce the material before starting a closed cut.

The script uses the rule that each closed cut shape requires one drilling point.

The script does not rely only on DXF entity types to decide whether a shape is closed.

This is important because a closed shape can be stored in many ways in a DXF file.

For example, a circle can be stored as a `CIRCLE` entity, but a rectangle can be stored as four independent `LINE` entities.

A closed decorative shape may also be stored as a `LWPOLYLINE`, a `POLYLINE`, arcs, splines, or a mix of several entities.

To handle these cases, the script first converts supported geometry into line segments, converts coordinates to millimetres, and snaps nearby endpoints together using:

```bash
--endpoint-tolerance
```

or:

```bash
-e
```

It then builds a graph:

- each snapped endpoint becomes a graph vertex
- each cut segment becomes a graph edge

Closed shapes are counted using the graph cycle count.

In simple terms, a cycle is a path that comes back to its starting point.

Each independent cycle is counted as one drilling point.

This means that a rectangle made from four separate `LINE` entities is counted as one closed shape, even though the DXF does not contain a single “rectangle” object.

The script uses the graph formula:

```text
cycles = edges - vertices + connected_components
```

for each connected component of the cutting graph.

This is more robust than simply checking whether every vertex has exactly two connected segments.

This matters for real laser DXF files because closed shapes can touch each other.

For example, two closed shapes may share one point or be connected by a small bridge.

With a simple degree-based method, the shared point would look like an ambiguous junction and the script could fail to count the closed shapes correctly.

With graph cycle counting, touching closed shapes can still be counted as separate drilling points when they create separate closed cycles.

Open contours are not counted as drilling points because they do not form a closed cut.

A single open line, an open arc, or a contour with a real gap will therefore add cutting length but no drilling point.

If a contour should be closed but is not counted as closed, it usually means that there is a small gap between endpoints.

In that case, increasing `--endpoint-tolerance / -e` may help.

Duplicate geometry can also affect drilling point detection.

If the same path is present twice, the cutting graph may contain repeated edges and extra junctions.

Using `--remove-duplicates / -d` removes duplicate segments from the analysis before graph cycles are counted.

Using `--write-dxf-noduplicate / -n / -w` can also create a cleaned DXF copy where complete duplicate entities have been removed.

---

## How cooling points are treated

### In short

- A cooling point is counted at a coordinate when at least one angle is below the configured threshold.
- The default threshold is `120°`.
- Only one cooling point is counted per coordinate.
- Multi-segment junctions are checked by comparing all incident segment pairs.
- Smooth curves and circles normally do not create cooling points unless their flattened segments form a sharp enough angle.

### Detailed explanation

A cooling point is counted when a coordinate has at least one geometric angle smaller than the configured threshold.

The default threshold is:

```text
120 degrees
```

This means that sharp corners such as 90° rectangle corners are counted as cooling points.

The threshold can be changed with:

```bash
--angle-threshold 120
```

or:

```bash
-a 120
```

The script checks angles at graph vertices.

If exactly two segments meet at a vertex, the script checks the angle between those two segments.

If three or more segments meet at the same vertex, the script checks all angle pairs and uses the smallest angle.

Only one cooling point is counted per coordinate, even if several angle pairs at that coordinate are below the threshold.

Smooth curves, circles, and rounded arcs normally do not create cooling points unless the flattened representation creates a sharp enough angle at a segment junction.

---

## Recommended diagnostic commands

Detailed report:

```bash
python dxf_laser_analyzer.py part.dxf --details
```

Detailed report with larger endpoint tolerance:

```bash
python dxf_laser_analyzer.py part.dxf --details --endpoint-tolerance 0.2
```

Analysis with duplicate removal:

```bash
python dxf_laser_analyzer.py part.dxf --remove-duplicates
```

Create cleaned no-duplicate DXF copy:

```bash
python dxf_laser_analyzer.py part.dxf --write-dxf-noduplicate
```

Equivalent short options:

```bash
python dxf_laser_analyzer.py part.dxf -v -e 0.2 -d
python dxf_laser_analyzer.py part.dxf -n
```

---

## Important limitations

- The script analyzes 2D XY geometry only.
- Curves are approximated by flattened line segments.
- The duplicate writer removes only complete duplicate entities.
- Partially overlapping entities are kept.
- Very small gaps can prevent contours from being detected as closed.
- Very large endpoint tolerance values may incorrectly merge nearby but distinct points.
- If a DXF file is unitless, the script assumes millimetres.
- Some complex or proprietary DXF entities may not be analyzable.
