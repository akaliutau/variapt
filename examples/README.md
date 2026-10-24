# Example inputs

`gardeyn4.json` is the full GARDEYN4 benchmark from the pinned Sparrow revision
`1fe3e3146110c34d77510636defb737c7ce893c6`. It contains 80 copies of five non-convex
part types, including detailed curved outlines represented as polygons. The JSON
is copied byte for byte: coordinates, strip height 1500, quantities and the four
allowed 90-degree rotations are unchanged. Omitted separation means zero; touching
is allowed. The upstream data does not specify physical units.

The [authors' paper, section 12](https://arxiv.org/html/2509.13329v2#S12) identifies
GARDEYN4–8 as derived from metalworking and shipbuilding shapes provided by AlmaCAM.
This is an industrially derived benchmark, not a claim about a specific production
order. [Pinned input source](https://github.com/JeroenGar/sparrow/blob/1fe3e3146110c34d77510636defb737c7ce893c6/data/input/gardeyn4.json).

`gardeyn4.source.json` records provenance, the input hash and retained constraints.
The upstream MIT notice is included in `Sparrow-LICENSE.txt`.

`repeated.json` and `tiny.json` are small synthetic smoke examples.
