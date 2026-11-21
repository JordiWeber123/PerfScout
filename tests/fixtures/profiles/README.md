# Profile fixtures

`papi-capture.json` contains the recorded event values from the [PAPI high-level documentation](https://github.com/icl-utk-edu/papi/wiki/PAPI-HL#possible-output-file). The CPU model and rates were removed.

`papi-threads.json` is synthetic. It exercises independent threads, an explicit rank, nested regions, intermediate reads, and counters above the IEEE 754 safe integer range.
