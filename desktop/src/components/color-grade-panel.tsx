import { useKinetographStore } from "@/store/use-kinetograph-store";
import { KinetographAPI } from "@/lib/api";
import { DEFAULT_COLOR_GRADE, type ColorGrade } from "@/types/kinetograph";
import { useCallback, useRef } from "react";
import { RotateCcw } from "lucide-react";

// ─── Slider config ─────────────────────────────────────────────────────────────

interface SliderDef {
	key: keyof ColorGrade;
	label: string;
	min: number;
	max: number;
	step: number;
	neutral: number;      // the "zero-effect" value
	unit?: string;
}

const SLIDERS: SliderDef[] = [
	{ key: "brightness",  label: "Brightness",  min: -1,   max: 1,   step: 0.01, neutral: 0 },
	{ key: "contrast",    label: "Contrast",    min: 0,    max: 3,   step: 0.01, neutral: 1 },
	{ key: "saturation",  label: "Saturation",  min: 0,    max: 3,   step: 0.01, neutral: 1 },
	{ key: "gamma",       label: "Gamma",       min: 0.1,  max: 5,   step: 0.01, neutral: 1 },
	{ key: "temperature", label: "Temperature", min: -1,   max: 1,   step: 0.01, neutral: 0 },
	{ key: "tint",        label: "Tint",        min: -1,   max: 1,   step: 0.01, neutral: 0 },
	{ key: "shadows",     label: "Shadows",     min: -1,   max: 1,   step: 0.01, neutral: 0 },
	{ key: "highlights",  label: "Highlights",  min: -1,   max: 1,   step: 0.01, neutral: 0 },
];

// ─── Component ─────────────────────────────────────────────────────────────────

export function ColorGradePanel() {
	const colorGrade = useKinetographStore((s) => s.colorGrade);
	const setColorGrade = useKinetographStore((s) => s.setColorGrade);
	const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

	// Debounced sync to backend
	const syncToBackend = useCallback((grade: ColorGrade) => {
		if (debounceRef.current) clearTimeout(debounceRef.current);
		debounceRef.current = setTimeout(() => {
			KinetographAPI.setColorGrade(grade).catch(() => {});
		}, 300);
	}, []);

	const handleChange = useCallback(
		(key: keyof ColorGrade, value: number) => {
			const updated = { ...colorGrade, [key]: value };
			setColorGrade({ [key]: value });
			syncToBackend(updated);
		},
		[colorGrade, setColorGrade, syncToBackend],
	);

	const handleReset = useCallback(() => {
		setColorGrade(DEFAULT_COLOR_GRADE);
		KinetographAPI.setColorGrade(DEFAULT_COLOR_GRADE).catch(() => {});
	}, [setColorGrade]);

	const isModified = SLIDERS.some(
		(s) => Math.abs(colorGrade[s.key] - s.neutral) > 0.001,
	);

	return (
		<div className="flex flex-col gap-1">
			{/* Header */}
			<div className="flex items-center justify-between px-1 mb-1">
				<span className="text-[10px] font-semibold text-zinc-300 uppercase tracking-wider">
					Color Grading
				</span>
				{isModified && (
					<button
						onClick={handleReset}
						className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[9px] text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800 transition-colors"
						title="Reset to defaults"
					>
						<RotateCcw className="h-2.5 w-2.5" />
						Reset
					</button>
				)}
			</div>

			{/* Sliders */}
			{SLIDERS.map((slider) => {
				const value = colorGrade[slider.key];
				const pct =
					((value - slider.min) / (slider.max - slider.min)) * 100;
				const neutralPct =
					((slider.neutral - slider.min) /
						(slider.max - slider.min)) *
					100;
				const isNeutral =
					Math.abs(value - slider.neutral) < slider.step * 0.5;

				return (
					<div key={slider.key} className="flex flex-col gap-0.5 px-1">
						<div className="flex items-center justify-between">
							<label className="text-[9px] text-zinc-500">
								{slider.label}
							</label>
							<span
								className={`text-[9px] font-mono tabular-nums ${
									isNeutral
										? "text-zinc-600"
										: "text-zinc-300"
								}`}
							>
								{value.toFixed(2)}
							</span>
						</div>
						<div className="relative h-3 flex items-center group">
							{/* Track background */}
							<div className="absolute inset-x-0 h-[3px] rounded-full bg-zinc-800" />
							{/* Neutral marker */}
							<div
								className="absolute h-[7px] w-[1px] bg-zinc-600 rounded-full"
								style={{ left: `${neutralPct}%` }}
							/>
							{/* Fill from neutral to current */}
							<div
								className="absolute h-[3px] rounded-full bg-blue-500/60"
								style={{
									left: `${Math.min(neutralPct, pct)}%`,
									width: `${Math.abs(pct - neutralPct)}%`,
								}}
							/>
							{/* Native range input */}
							<input
								type="range"
								min={slider.min}
								max={slider.max}
								step={slider.step}
								value={value}
								onChange={(e) =>
									handleChange(
										slider.key,
										parseFloat(e.target.value),
									)
								}
								onDoubleClick={() =>
									handleChange(slider.key, slider.neutral)
								}
								className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
							/>
							{/* Custom thumb */}
							<div
								className="absolute h-2.5 w-2.5 rounded-full border border-zinc-500 bg-zinc-300 group-hover:border-blue-400 group-hover:bg-white transition-colors pointer-events-none"
								style={{
									left: `calc(${pct}% - 5px)`,
								}}
							/>
						</div>
					</div>
				);
			})}
		</div>
	);
}
