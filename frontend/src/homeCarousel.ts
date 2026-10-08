export type CarouselSlot = "previous" | "current" | "next" | "hidden";

export function circularIndex(index: number, step: number, itemCount: number) {
  if (itemCount <= 0) return 0;
  return (index + step + itemCount) % itemCount;
}

export function caseSlot(itemIndex: number, currentIndex: number, itemCount: number): CarouselSlot {
  if (itemCount <= 0) return "hidden";
  if (itemIndex === currentIndex) return "current";
  if (itemIndex === circularIndex(currentIndex, -1, itemCount)) return "previous";
  if (itemIndex === circularIndex(currentIndex, 1, itemCount)) return "next";
  return "hidden";
}

export function initialCaseIndex<T extends { code: string }>(
  items: T[],
  currentCode: string | null | undefined
) {
  if (!currentCode) return 0;
  const matchedIndex = items.findIndex((item) => item.code === currentCode);
  return matchedIndex >= 0 ? matchedIndex : 0;
}

export function shouldAutoAdvance({
  itemCount,
  paused,
  focusPaused,
  hoverPaused,
  reducedMotion
}: {
  itemCount: number;
  paused: boolean;
  focusPaused: boolean;
  hoverPaused: boolean;
  reducedMotion: boolean;
}) {
  return itemCount > 1 && !paused && !focusPaused && !hoverPaused && !reducedMotion;
}

export function uniquePartScopes<T extends { asset_kind: string; target_part_code: string }>(
  scopes: T[]
): T[] {
  const seen = new Set<string>();
  return scopes.filter((scope) => {
    const key = `${scope.asset_kind}:${scope.target_part_code}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
