#!/usr/bin/env python3
"""Convert a local Zarr v2 store into an EOxElements-compatible Zarr v3 GeoZarr store.

The data, chunking, coordinates and attributes are copied as they are. The
root group additionally gets the `spatial:` / `proj:` attributes that
OpenLayers' `ol/source/GeoZarr` (used by `<eox-map>`) needs to open the store
with `bands: [...]`, so every 2-D+ data variable can be selected in
`<eox-layercontrol>`:

    spatial:dimensions  [y_dim, x_dim]
    spatial:shape       [ny, nx]
    spatial:bbox        [xmin, ymin, xmax, ymax]  (pixel edges)
    spatial:transform   [a, b, c, d, e, f]        (affine, encodes y orientation)
    proj:code           "EPSG:xxxx"
    proj:wkt2           (when pyproj is installed)

Each data variable without `valid_range` / `actual_range` also gets an
`actual_range` [min, max], which the layer control uses for its min/max sliders
(skip with --no-ranges).

The EPSG code is read from the store (`spatial_ref` / `crs` grid-mapping
variable, `grid_mapping`, `crs_wkt`, `proj:code`, `epsg` attributes). If it
cannot be found, pass it with --epsg.

Usage:
    python convert_zarr.py INPUT.zarr OUTPUT.zarr [--epsg 4326] [--overwrite]
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import xarray as xr
import zarr

X_NAMES = ("x", "lon", "longitude", "nav_lon", "rlon")
Y_NAMES = ("y", "lat", "latitude", "nav_lat", "rlat")
GRID_MAPPING_NAMES = ("spatial_ref", "crs", "grid_mapping", "projection")
# v2-only encodings that cannot be written to a v3 store
V2_ONLY_ENCODINGS = ("compressor", "compressors", "filters", "serializer","object_codec", "dimension_separator", "preferred_chunks")


def fail(msg: str) -> None:
    sys.exit(f"ERROR: {msg}")


def parse_epsg(value) -> int | None:
    """Turn 4326 / '4326' / 'EPSG:4326' / a WKT or CRS string into an EPSG code."""
    if value is None:
        return None
    if isinstance(value, (int, np.integer)):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = re.fullmatch(r"(?i)epsg:(\d+)", text)
    if m:
        return int(m.group(1))
    try:
        from pyproj import CRS

        return CRS.from_user_input(text).to_epsg()
    except Exception:
        pass
    # Fallback without pyproj: the outermost AUTHORITY / ID in a WKT is the CRS's own
    ids = re.findall(r'(?:AUTHORITY|ID)\["EPSG",\s*"?(\d+)"?\]', text)
    return int(ids[-1]) if ids else None


def detect_epsg(ds: xr.Dataset) -> tuple[int | None, str]:
    """Look for the CRS in the usual CF / rioxarray / GeoZarr places."""
    candidates: list[tuple[str, object]] = []
    for key in ("proj:code", "proj:epsg", "epsg", "crs", "spatial_ref", "crs_wkt"):
        if key in ds.attrs:
            candidates.append((f"root attribute '{key}'", ds.attrs[key]))

    grid_mappings = {v.attrs["grid_mapping"] for v in ds.data_vars.values() if "grid_mapping" in v.attrs}
    for name in [*grid_mappings, *GRID_MAPPING_NAMES]:
        if name in ds.variables:
            attrs = ds[name].attrs
            for key in ("crs_wkt", "spatial_ref", "epsg_code", "epsg"):
                if key in attrs:
                    candidates.append((f"'{name}' attribute '{key}'", attrs[key]))

    for where, value in candidates:
        epsg = parse_epsg(value)
        if epsg:
            return epsg, where
    return None, ""


def find_spatial_dims(ds: xr.Dataset) -> tuple[str, str]:
    dims = {d.lower(): d for d in ds.dims}
    x = next((dims[n] for n in X_NAMES if n in dims), None)
    y = next((dims[n] for n in Y_NAMES if n in dims), None)
    if not x or not y:
        fail(
            f"could not find the x/y dimensions among {list(ds.dims)}; "
            f"expected one of {X_NAMES} and one of {Y_NAMES}."
        )
    for name in (x, y):
        if name not in ds.coords or ds[name].ndim != 1 or ds.sizes[name] < 2:
            fail(f"dimension '{name}' needs a 1-D coordinate array with at least 2 values.")
    return y, x


def spatial_attrs(ds: xr.Dataset, y: str, x: str, epsg: int) -> dict:
    xs = ds[x].values.astype("float64")
    ys = ds[y].values.astype("float64")
    nx, ny = xs.size, ys.size
    dx = (xs[-1] - xs[0]) / (nx - 1)
    dy = (ys[-1] - ys[0]) / (ny - 1)
    if dx <= 0:
        fail(f"x coordinate '{x}' is descending; OpenLayers GeoZarr does not support that.")
    for name, vals, step in ((x, xs, dx), (y, ys, dy)):
        if not np.allclose(np.diff(vals), step, rtol=1e-3, atol=0):
            print(f"  [WARN] '{name}' is not evenly spaced; the map will place pixels on a regular grid.")

    xmin, xmax = xs.min() - abs(dx) / 2, xs.max() + abs(dx) / 2
    ymin, ymax = ys.min() - abs(dy) / 2, ys.max() + abs(dy) / 2
    # Affine of the first pixel's corner: north-up (descending y) -> e < 0
    y_origin = ys[0] - dy / 2
    attrs = {
        "spatial:dimensions": [y, x],
        "spatial:shape": [int(ny), int(nx)],
        "spatial:bbox": [float(xmin), float(ymin), float(xmax), float(ymax)],
        "spatial:transform": [float(dx), 0.0, float(xs[0] - dx / 2), 0.0, float(dy), float(y_origin)],
        "proj:code": f"EPSG:{epsg}",
    }
    try:
        from pyproj import CRS

        attrs["proj:wkt2"] = CRS.from_epsg(epsg).to_wkt()
    except Exception:
        pass
    return attrs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", type=Path, help="local Zarr v2 store (directory)")
    parser.add_argument("output", type=Path, help="local path for the new Zarr v3 store")
    parser.add_argument("--epsg", help="CRS of the data (e.g. 4326 or EPSG:4326); required if not found in the store")
    parser.add_argument("--overwrite", action="store_true", help="replace OUTPUT if it already exists")
    parser.add_argument("--no-ranges", action="store_true", help="do not compute actual_range per variable")
    args = parser.parse_args()

    src, dst = args.input.expanduser().resolve(), args.output.expanduser().resolve()
    if not src.is_dir():
        fail(f"input store {src} does not exist (or is not a local directory).")
    if not ((src / ".zgroup").is_file() or (src / ".zmetadata").is_file()):
        if (src / "zarr.json").is_file():
            fail(f"{src} is already a Zarr v3 store.")
        fail(f"{src} does not look like a Zarr v2 store (no .zgroup / .zmetadata).")
    if dst == src or src in dst.parents:
        fail("output must not be the input store or inside it.")
    if dst.exists():
        if not args.overwrite:
            fail(f"output {dst} already exists; pass --overwrite to replace it.")
        shutil.rmtree(dst)

    try:
        import dask  # noqa: F401

        chunks = {}  # keep the store's own chunks, stream instead of loading into memory
    except ImportError:
        chunks = None
    ds = xr.open_zarr(src, chunks=chunks, consolidated=None, zarr_format=2, decode_times=False, mask_and_scale=False)
    print(f"Opened {src}: {len(ds.data_vars)} data variables, dims {dict(ds.sizes)}")

    y, x = find_spatial_dims(ds)

    if args.epsg:
        epsg = parse_epsg(args.epsg)
        if not epsg:
            fail(f"could not understand --epsg {args.epsg!r}; use e.g. 4326 or EPSG:4326.")
        print(f"  EPSG:{epsg} (from --epsg)")
    else:
        epsg, where = detect_epsg(ds)
        if not epsg:
            fail("could not find the EPSG code in the store; pass it with --epsg, e.g. --epsg 4326.")
        print(f"  EPSG:{epsg} (from {where})")

    renderable = [name for name, v in ds.data_vars.items() if y in v.dims and x in v.dims]
    if not renderable:
        fail(f"no data variable has both '{y}' and '{x}' dimensions.")
    for name in renderable:
        if ds[name].dims[-2:] != (y, x):
            print(f"  [WARN] '{name}' has dims {ds[name].dims}; '{y}', '{x}' should be the last two.")

    # Strip v2-only codecs; keep chunks, dtype, fill value and CF scaling as stored
    for var in ds.variables.values():
        for key in V2_ONLY_ENCODINGS:
            var.encoding.pop(key, None)

    if not args.no_ranges:
        for name in renderable:
            attrs = ds[name].attrs
            if any(k in attrs for k in ("valid_range", "actual_range", "valid_min_max")) or (
                "valid_min" in attrs and "valid_max" in attrs
            ):
                continue
            data = ds[name]
            fill = data.encoding.get("_FillValue", attrs.get("_FillValue"))
            if fill is not None and not (isinstance(fill, float) and np.isnan(fill)):
                data = data.where(data != fill)
            lo, hi = data.min(skipna=True).values, data.max(skipna=True).values
            if np.isfinite(lo) and np.isfinite(hi):
                attrs["actual_range"] = [float(lo), float(hi)]
                print(f"  {name}: actual_range [{float(lo):.6g}, {float(hi):.6g}]")

    ds.attrs.update(spatial_attrs(ds, y, x, epsg))

    print(f"Writing {dst} ...")
    ds.to_zarr(dst, mode="w", zarr_format=3, consolidated=True)
    zarr.consolidate_metadata(str(dst))

    print(
        f"Done: {dst}\n"
        f"  Use it in local.ipynb with local_zarr_rel = '<path relative to $HOME>' and\n"
        f"  variable = one of {renderable}"
    )


if __name__ == "__main__":
    main()
