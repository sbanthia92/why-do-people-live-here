# Map data

`natural_earth.json.gz` holds coastlines, large lakes and major cities from
[Natural Earth](https://www.naturalearthdata.com/), which is in the public domain: "You
may use the maps in any manner, including modifying the content and design." No credit is
required.

It was built from the 1:110m and 1:50m land polygons, the 1:50m lakes (largest only) and
the 1:110m populated places, keeping coordinates only and rounding them to two decimal
places (about 1 km). `pipeline/map_card.py` draws from it.
