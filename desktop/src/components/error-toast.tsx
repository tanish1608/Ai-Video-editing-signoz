import { useKinetographStore } from "@/store/use-kinetograph-store";
import { X, AlertCircle } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import { useState } from "react";

export function ErrorToast() {
	const errors = useKinetographStore((s) => s.errors);
	const clearErrors = useKinetographStore((s) => s.clearErrors);
	const [dismissed, setDismissed] = useState<Set<number>>(new Set());

	const visible = errors.filter((_, i) => !dismissed.has(i));
	if (visible.length === 0) return null;

	return (
		<div className="fixed top-12 right-4 z-50 flex flex-col gap-2 max-w-sm">
			<AnimatePresence>
				{errors.map((err, i) => {
					if (dismissed.has(i)) return null;
					return (
						<motion.div
							key={i}
							initial={{ opacity: 0, x: 20 }}
							animate={{ opacity: 1, x: 0 }}
							exit={{ opacity: 0, x: 20 }}
							className="flex items-start gap-2 rounded border border-red-800/30 bg-red-950/50 p-3 backdrop-blur-sm"
						>
							<AlertCircle className="h-3.5 w-3.5 text-red-400 shrink-0 mt-0.5" />
							<div className="flex-1 min-w-0">
								<p className="text-[10px] font-medium text-red-300">{err.message}</p>
								{err.details && (
									<p className="mt-1 text-[9px] text-red-400/70 truncate">{err.details}</p>
								)}
							</div>
							<button
								onClick={() => setDismissed((s) => new Set(s).add(i))}
								className="text-red-500/50 hover:text-red-400 transition-colors shrink-0"
							>
								<X className="h-3 w-3" />
							</button>
						</motion.div>
					);
				})}
			</AnimatePresence>
			{visible.length > 1 && (
				<button
					onClick={() => { clearErrors(); setDismissed(new Set()); }}
					className="text-[9px] text-zinc-500 hover:text-zinc-300 transition-colors self-end"
				>
					Dismiss all
				</button>
			)}
		</div>
	);
}
