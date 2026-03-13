
#!/usr/bin/env python3
"""
Resolume DMX Shortcuts Cloner — PRESERVE MODE (stream-safe)
- Operates on raw text: preserves odd attrs (e.g., 9bit="1") and original whitespace exactly.
- Streams through the file and, for each <Shortcut> block of the single source layer,
  writes the original block THEN all clone blocks, avoiding position-shift bugs.
- Tight packing across 512-channel lumiverses (analog uses 2 channels).

Usage:
  python3 resolume_dmx_clone_preserve.py "Source.xml" "Dest.xml" --layers 23
  python3 resolume_dmx_clone_preserve.py "Source.xml" "Dest.xml" --layers 23 --dry-run --print-all
"""

import argparse, re
from pathlib import Path

LOW_MASK = (1 << 18) - 1
CH_MASK  = 0x1FF

LAYER_RE = re.compile(r"/layers/(\d+)/")
# Capture each full Shortcut block (non-greedy) with name="Shortcut"
SHORTCUT_BLOCK_RE = re.compile(r'(<Shortcut\b[^>]*name="Shortcut"[^>]*>.*?</Shortcut>)', re.DOTALL)
INPUTPATH_RE = re.compile(r'(<ShortcutPath\b[^>]*name="InputPath"[^>]*path=")([^"]+)(")')
KEY_RE = re.compile(r'(key=")(\d+)(")')
UNIQUEID_RE = re.compile(r'(uniqueId=")(\d+)(")')

def decode_key(k: int):
    base = k & ~LOW_MASK
    analog = (k >> 17) & 1
    lum = ((k >> 9) & 0xFF) + 1
    ch = (k & CH_MASK) + 1
    return base, lum, analog, ch

def encode_key(base: int, lum: int, analog: int, ch: int) -> int:
    return (base & ~LOW_MASK) | (((lum - 1) & 0xFF) << 9) | ((analog & 1) << 17) | ((ch - 1) & CH_MASK)

def extract_shortcuts_for_single_layer(raw: str):
    blocks = []
    for m in SHORTCUT_BLOCK_RE.finditer(raw):
        block = m.group(1)
        # InputPath path
        m_ip = INPUTPATH_RE.search(block)
        if not m_ip:
            continue
        path = m_ip.group(2)
        m_layer = LAYER_RE.search(path or "")
        if not m_layer:
            continue
        layer_idx = int(m_layer.group(1))
        # Key
        mkey = KEY_RE.search(block)
        if not mkey:
            continue
        key = int(mkey.group(2))
        blocks.append((m.span(1), block, layer_idx, key, path))
    if not blocks:
        raise ValueError("No <Shortcut> blocks with InputPath and key found.")
    # Ensure single layer
    uniq_layers = {b[2] for b in blocks}
    if len(uniq_layers) != 1:
        raise ValueError(f"Expected exactly one layer in source; found layers: {sorted(uniq_layers)}")
    src_layer = blocks[0][2]
    return blocks, src_layer

def collect_layer_width(blocks):
    min_slot = 512
    max_slot = 1
    for _, blk, _, key, _ in blocks:
        base, lum, analog, ch = decode_key(key)
        if analog == 1 and ch < 512:
            max_slot = max(max_slot, ch + 1)
        else:
            max_slot = max(max_slot, ch)
        min_slot = min(min_slot, ch)
    return max(min_slot,1), max_slot

def infer_base_lum(blocks):
    # take from the first block
    key = blocks[0][3]
    base, lum, analog, ch = decode_key(key)
    return base, lum

def bump_unique(block: str, next_uid: int) -> (str, int):
    if UNIQUEID_RE.search(block):
        block = UNIQUEID_RE.sub(rf'\g<1>{next_uid}\g<3>', block, count=1)
    else:
        block = re.sub(r'(<Shortcut\b[^>]*?)>', rf'\1 uniqueId="{next_uid}">', block, count=1)
    return block, next_uid + 1

def repl_path(block: str, from_layer: int, to_layer: int) -> str:
    def _sub(m):
        prefix, path, suffix = m.groups()
        new_path = path.replace(f"/layers/{from_layer}/", f"/layers/{to_layer}/")
        return f"{prefix}{new_path}{suffix}"
    return INPUTPATH_RE.sub(_sub, block, count=1)

def set_key(block: str, new_key: int) -> str:
    return KEY_RE.sub(rf'\g<1>{new_key}\g<3>', block, count=1)

def compute_clone_positions(blocks, total_layers, layer_width, src_layer, lum0):
    """Return list of (target_layer, target_lum, group_idx) for each clone i=1..(layers-1)."""
    def would_overflow(gidx: int) -> bool:
        for _, blk, _, key, _ in blocks:
            base, lum, analog, ch0 = decode_key(key)
            width = 2 if analog == 1 else 1
            desired = (ch0 - 1) + gidx * layer_width + 1
            if desired + width - 1 > 512:
                return True
        return False

    plan = []
    current_lum = lum0
    # Determine where the source layer sits within its lumiverse block grid
    min_slot, _ = collect_layer_width(blocks)
    start_block_idx = (min_slot - 1) // layer_width
    gidx = start_block_idx + 1  # first clone begins after source block
    clones_needed = total_layers - 1
    for i in range(1, clones_needed + 1):
        if would_overflow(gidx):
            current_lum += 1
            gidx = 0
        target_layer = src_layer + i
        plan.append((target_layer, current_lum, gidx))
        gidx += 1
    return plan

def main():
    ap = argparse.ArgumentParser(description="Clone Resolume DMX shortcuts (preserve raw XML; stream-safe).")
    ap.add_argument("input_xml")
    ap.add_argument("output_xml")
    ap.add_argument("--layers", type=int, required=True, help="Total layers (including source)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-all", action="store_true", help="In dry-run, print every planned mapping line")
    args = ap.parse_args()

    raw = Path(args.input_xml).read_text(encoding="utf-8", errors="ignore")

    # Gather blocks for the single source layer
    blocks, src_layer = extract_shortcuts_for_single_layer(raw)
    min_slot, max_slot = collect_layer_width(blocks)
    layer_width = max_slot
    base0, lum0 = infer_base_lum(blocks)

    # Compute per-clone placement (layer number, lumiverse, group idx)
    if args.layers < 1:
        raise ValueError("--layers must be >= 1")
    plan = compute_clone_positions(blocks, args.layers, layer_width, src_layer, lum0)

    # Build a global uniqueId counter
    uids = [int(m.group(2)) for m in UNIQUEID_RE.finditer(raw) if m.group(2).isdigit()]
    next_uid = (max(uids) + 1) if uids else 1

    # DRY RUN
    if args.dry_run:
        print(f"[dmxclone] source layer={src_layer} base={hex(base0)} lum={lum0}")
        print(f"[dmxclone] layer span {min_slot}..{max_slot} -> width={layer_width}, start_block={(min_slot-1)//layer_width}")
        shown = 0
        for (target_layer, lum, gidx) in plan:
            for _, blk, _, key, path in blocks:
                base, lum_s, analog, ch0 = decode_key(key)
                desired_ch = (ch0 - 1) + gidx * layer_width + 1
                if args.print_all or shown < 8:
                    print(f"{path.replace(f'/layers/{src_layer}/', f'/layers/{target_layer}/')}  ->  L={lum} ch={desired_ch}  ({'analog' if analog else 'switch'})")
                    shown += 1
        print(f"[dmxclone] planned clones for layers {src_layer+1}..{src_layer + args.layers - 1}")
        if not args.print_all and shown >= 8:
            print("... (use --print-all to show every line)")
        return

    # STREAM-SAFE WRITE: iterate original file, and after each source block, append its clones
    out_parts = []
    pos = 0
    # We'll reuse per-block clones per target, but we need fresh uniqueIds per clone instance
    for m in SHORTCUT_BLOCK_RE.finditer(raw):
        start, end = m.span(1)
        block = m.group(1)

        # Check if this block belongs to the source layer
        m_ip = INPUTPATH_RE.search(block)
        belongs = False
        if m_ip:
            path = m_ip.group(2)
            ml = LAYER_RE.search(path or "")
            belongs = (ml and int(ml.group(1)) == src_layer)

        out_parts.append(raw[pos:end])  # original up to end of this block
        pos = end

        if not belongs:
            continue

        # Prepare clones for this block for each planned layer
        # NOTE: We must not modify 'block' in-place; we copy/modify for each clone.
        mkey = KEY_RE.search(block)
        key0 = int(mkey.group(2)) if mkey else None
        base_s, lum_s, analog, ch0 = decode_key(key0) if key0 is not None else (0,0,0,1)

        clones_text = []
        for (target_layer, lum, gidx) in plan:
            desired_ch = (ch0 - 1) + gidx * layer_width + 1
            new_key = encode_key(base0, lum, analog, desired_ch)
            new_blk = block
            # 1) path
            new_blk = repl_path(new_blk, src_layer, target_layer)
            # 2) key
            new_blk = set_key(new_blk, new_key)
            # 3) uniqueId
            new_blk, next_uid = bump_unique(new_blk, next_uid)
            clones_text.append(new_blk)

        out_parts.append("".join(clones_text))

    out_parts.append(raw[pos:])
    out_text = "".join(out_parts)
    Path(args.output_xml).write_text(out_text, encoding="utf-8")
    print(f"[dmxclone] Wrote {args.output_xml} (layers={args.layers})")

if __name__ == "__main__":
    main()
