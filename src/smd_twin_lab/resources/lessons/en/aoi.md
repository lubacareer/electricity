# AOI and a digital twin answer different questions

Automated optical inspection is strong at visible assembly evidence: presence, alignment,
polarity, solder coverage, and recognizable package defects. It cannot directly prove that a net
has the expected voltage or that firmware reacts correctly.

The board canvas lets you move from a physical reference designator to its electrical nets. A
simulated fault then demonstrates a possible functional consequence. Treat that link as a
testable hypothesis, not as a guarantee that every physical defect behaves identically.

Useful questions while inspecting a component:

- Is it visible from the selected board side?
- Is polarity meaningful for this package?
- Which joints or neighboring parts could hide it?
- Which functional measurement would confirm the suspected defect?
