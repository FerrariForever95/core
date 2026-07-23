"""
pystone_lowmem.py – MicroPython benchmark for low-memory devices
Based on the original Python Pystone benchmark.
"""

import time

LOOPS = 1000

def pystones(loops=LOOPS):
    start_time = time.ticks_us()
    benchtime, stones = Proc0(loops)
    total_time = time.ticks_diff(time.ticks_us(), start_time)
    pystones_per_sec = (loops * 1_000_000) / total_time
    print("Pystone(1.1) time for %d passes = %.3f sec" % (loops, total_time / 1_000_000))
    print("This machine benchmarks at %.1f pystones/second" % pystones_per_sec)
    return pystones_per_sec


def Proc0(loops):
    IntGlob = 0
    BoolGlob = False
    Char1Glob = 'A'
    Char2Glob = 'B'
    Array1Glob = [0]*51
    Array2Glob = [[0]*51 for _ in range(51)]
    EnumGlob = 0

    def Proc1(PtrParIn):
        return PtrParIn

    for i in range(loops):
        IntLoc1 = 2
        IntLoc2 = 3
        String1Loc = "A"
        String2Loc = "B"
        if String1Loc < String2Loc:
            BoolLoc = True
        else:
            BoolLoc = False
        IntLoc3 = IntLoc2 * IntLoc1
        IntLoc2 = IntLoc3 / (IntLoc1 + 1)
    return (loops, loops)


def main(loops=LOOPS):
    print(" benchmark (lowmem)")
    pystones(loops)


if __name__ == "__main__":
    main()




