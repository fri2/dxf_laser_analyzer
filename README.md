# dxf_laser_analyzer
Command-line tool to analyze and correct DXF geometry for laser cutting.

Command-line tool to analyze and correct DXF geometry for laser cutting.

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
