import { useCallback, useRef, useState } from "react";

/**
 * Marquee (rubber-band / lasso) selection hook.
 *
 * Returns a `marquee` rect in **viewport-space** (relative to the
 * container's visible bounding rect) so the overlay can be positioned
 * with `position: absolute` inside the container without scroll-offset
 * math. Hit-testing is done in scroll-space internally.
 *
 * Uses document-level listeners for pointermove/pointerup to avoid
 * issues with pointer capture, native drag, and dnd-kit conflicts.
 */

export interface MarqueeRect {
	/** Left edge relative to container's visible viewport */
	x: number;
	/** Top edge relative to container's visible viewport */
	y: number;
	width: number;
	height: number;
}

interface UseMarqueeSelectOptions {
	/** Ref to the scrollable container element */
	containerRef: React.RefObject<HTMLElement | null>;
	/** CSS selector for selectable items inside the container */
	itemSelector: string;
	/** data-attribute on selectable items that holds their ID */
	idAttribute: string;
	/** Called when the set of marquee-selected IDs changes */
	onSelectionChange: (ids: Set<string>) => void;
	/** Optional predicate — return false to prevent marquee start (e.g. if user clicked a button) */
	shouldStart?: (e: React.PointerEvent) => boolean;
	/** Minimum drag distance (px) before marquee activates. Default 5. */
	threshold?: number;
}

export function useMarqueeSelect({
	containerRef,
	itemSelector,
	idAttribute,
	onSelectionChange,
	shouldStart,
	threshold = 5,
}: UseMarqueeSelectOptions) {
	const [marquee, setMarquee] = useState<MarqueeRect | null>(null);
	const isSelectingRef = useRef(false);
	const activatedRef = useRef(false);
	const additiveRef = useRef(false);
	const baseSelectionRef = useRef<Set<string>>(new Set());
	/** Origin in viewport-space (for overlay positioning) */
	const originVpRef = useRef({ x: 0, y: 0 });
	/** Origin in scroll-space (for hit-testing) */
	const originScrollRef = useRef({ x: 0, y: 0 });

	const handlePointerDown = useCallback(
		(e: React.PointerEvent) => {
			if (e.button !== 0) return;
			if (shouldStart && !shouldStart(e)) return;

			const container = containerRef.current;
			if (!container) return;

			additiveRef.current = e.metaKey || e.ctrlKey || e.shiftKey;
			baseSelectionRef.current = additiveRef.current
				? new Set(
						Array.from(
							container.querySelectorAll(`${itemSelector}[data-selected="true"]`),
						).map((el) => el.getAttribute(idAttribute) ?? ""),
				  )
				: new Set();

			const rect = container.getBoundingClientRect();
			originVpRef.current = {
				x: e.clientX - rect.left,
				y: e.clientY - rect.top,
			};
			originScrollRef.current = {
				x: e.clientX - rect.left + container.scrollLeft,
				y: e.clientY - rect.top + container.scrollTop,
			};

			isSelectingRef.current = true;
			activatedRef.current = false;

			// ── Document-level listeners ──────────────────────────────
			const cleanup = () => {
				isSelectingRef.current = false;
				setMarquee(null);
				document.removeEventListener("pointermove", onMove);
				document.removeEventListener("pointerup", cleanup);
				document.removeEventListener("pointercancel", cleanup);
			};

			const onMove = (ev: PointerEvent) => {
				if (!isSelectingRef.current) return;
				const c = containerRef.current;
				if (!c) return;
				const r = c.getBoundingClientRect();

				// Viewport-space position (for the visual overlay)
				const vpX = ev.clientX - r.left;
				const vpY = ev.clientY - r.top;
				const dx = vpX - originVpRef.current.x;
				const dy = vpY - originVpRef.current.y;

				if (!activatedRef.current) {
					if (Math.abs(dx) < threshold && Math.abs(dy) < threshold) return;
					activatedRef.current = true;
				}

				setMarquee({
					x: Math.min(originVpRef.current.x, vpX),
					y: Math.min(originVpRef.current.y, vpY),
					width: Math.abs(dx),
					height: Math.abs(dy),
				});

				// Scroll-space position (for hit-testing against items)
				const scrollX = ev.clientX - r.left + c.scrollLeft;
				const scrollY = ev.clientY - r.top + c.scrollTop;
				const hitX = Math.min(originScrollRef.current.x, scrollX);
				const hitY = Math.min(originScrollRef.current.y, scrollY);
				const hitW = Math.abs(scrollX - originScrollRef.current.x);
				const hitH = Math.abs(scrollY - originScrollRef.current.y);

				const items = c.querySelectorAll(itemSelector);
				const selected = new Set(baseSelectionRef.current);

				for (const item of items) {
					const id = item.getAttribute(idAttribute);
					if (!id) continue;
					const ir = item.getBoundingClientRect();
					const ix = ir.left - r.left + c.scrollLeft;
					const iy = ir.top - r.top + c.scrollTop;
					if (
						hitX < ix + ir.width &&
						hitX + hitW > ix &&
						hitY < iy + ir.height &&
						hitY + hitH > iy
					) {
						selected.add(id);
					}
				}

				onSelectionChange(selected);
			};

			document.addEventListener("pointermove", onMove);
			document.addEventListener("pointerup", cleanup);
			document.addEventListener("pointercancel", cleanup);
		},
		[containerRef, idAttribute, itemSelector, onSelectionChange, shouldStart, threshold],
	);

	return {
		/** The marquee rect in viewport-space (relative to container), or null */
		marquee,
		/** Whether a marquee drag is active (past threshold) */
		isActive: marquee !== null,
		/** Attach onPointerDown to the container element */
		handlers: {
			onPointerDown: handlePointerDown,
		},
	};
}
