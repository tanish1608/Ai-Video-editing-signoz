import { useState, useEffect, useCallback } from "react";
import { KinetographAPI } from "@/lib/api";
import { CaptionStylePreset } from "@/types/kinetograph";
import { useKinetographStore } from "@/store/use-kinetograph-store";
import { useChatStore } from "@/store/use-chat-store";
import { Type, Loader2, Check, Sparkles, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Convert ASS colour (&HAABBGGRR& or &HBBGGRR) to a CSS hex colour.
 * ASS stores colours in reverse-byte-order: Blue-Green-Red, with an
 * optional leading alpha pair.
 */
function assColorToCss(assColor: string): string {
  const hex = assColor.replace(/&H|&/g, "");
  if (hex.length >= 6) {
    // Take the last 6 chars → BBGGRR
    const tail = hex.slice(-6);
    const bb = tail.slice(0, 2);
    const gg = tail.slice(2, 4);
    const rr = tail.slice(4, 6);
    return `#${rr}${gg}${bb}`;
  }
  return "#ffffff";
}

export function CaptionsPanel() {
  const [styles, setStyles] = useState<CaptionStylePreset[]>([]);
  const [selectedStyleId, setSelectedStyleId] = useState<string>("bold-yellow");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const renderUrl = useKinetographStore((s) => s.renderUrl);
  const paperEdit = useKinetographStore((s) => s.paperEdit);

  // Fetch available caption style presets from backend
  useEffect(() => {
    KinetographAPI.getCaptionStyles()
      .then((res) => {
        setStyles(res.styles);
        if (res.styles.length > 0 && !res.styles.find((s) => s.id === selectedStyleId)) {
          setSelectedStyleId(res.styles[0].id);
        }
      })
      .catch(() => {
        // Fallback: show a few hardcoded presets when backend is unreachable
        setStyles([]);
      });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleGenerate = useCallback(async () => {
    setGenerating(true);
    setError(null);
    try {
      // 1. Select the caption style on the backend
      await KinetographAPI.selectCaptionStyle(selectedStyleId);

      // 2. Trigger the edit pipeline starting from the captioner agent
      const chat = useChatStore.getState();
      chat.setPipelineActive(true);
      chat.setProcessing(true);
      chat.addUserMessage("Generate captions with style: " + selectedStyleId);

      await KinetographAPI.editPipeline({
        instruction: `generate captions with style ${selectedStyleId}`,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Caption generation failed";
      setError(msg);
    } finally {
      setGenerating(false);
    }
  }, [selectedStyleId]);

  const hasClips = paperEdit && paperEdit.clips.length > 0;

  return (
    <div className="flex flex-col gap-3 h-full overflow-y-auto custom-scrollbar">
      {/* Header */}
      <div className="flex items-center gap-2">
        <Type className="h-3.5 w-3.5 text-blue-500" />
        <span className="text-[10px] font-semibold text-zinc-300">Captions</span>
      </div>

      <p className="text-[9px] text-zinc-500 leading-relaxed">
        Choose a caption style and generate word-by-word animated captions for your video.
      </p>

      {/* Style presets grid */}
      <div className="flex flex-col gap-1.5">
        <span className="text-[9px] font-medium text-zinc-500 uppercase tracking-wider">
          Style Presets
        </span>
        <div className="flex flex-col gap-1.5">
          {styles.map((style) => {
            const activeTextColor = assColorToCss(style.active_color);
            const inactiveTextColor = assColorToCss(style.inactive_color);
            const bgColor = assColorToCss(style.bg_color);
            const isSelected = selectedStyleId === style.id;

            return (
              <button
                key={style.id}
                onClick={() => setSelectedStyleId(style.id)}
                className={cn(
                  "flex flex-col gap-1.5 rounded-lg border p-2.5 text-left transition-all",
                  isSelected
                    ? "border-blue-500 bg-blue-500/10 ring-1 ring-blue-500/30"
                    : "border-zinc-800 bg-zinc-900/50 hover:border-zinc-600 hover:bg-zinc-800/50",
                )}
              >
                {/* Title row */}
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-xs">{style.preview.split(" ")[0]}</span>
                    <span className="text-[10px] font-medium text-zinc-300">
                      {style.name}
                    </span>
                  </div>
                  {isSelected && <Check className="h-3 w-3 text-blue-400" />}
                </div>

                {/* Visual caption preview */}
                <div className="flex items-center gap-1.5 mt-0.5">
                  <div
                    className="rounded px-2 py-1 text-[10px] font-bold"
                    style={{
                      backgroundColor:
                        style.border_style === 4
                          ? bgColor + "cc" // semi-opaque
                          : "transparent",
                      border:
                        style.border_style !== 4
                          ? `1px solid ${assColorToCss(style.outline_color)}60`
                          : "none",
                    }}
                  >
                    <span style={{ color: inactiveTextColor }}>Hello </span>
                    <span
                      style={{
                        color: activeTextColor,
                        fontWeight: 700,
                        transform: "scale(1.1)",
                        display: "inline-block",
                      }}
                    >
                      World
                    </span>
                    <span style={{ color: inactiveTextColor }}> Today</span>
                  </div>
                </div>

                {/* Metadata badges */}
                <div className="flex items-center gap-1 mt-0.5 flex-wrap">
                  <span className="text-[7px] font-mono text-zinc-600 bg-zinc-800/80 px-1 py-0.5 rounded">
                    {style.font_size}pt
                  </span>
                  <span className="text-[7px] font-mono text-zinc-600 bg-zinc-800/80 px-1 py-0.5 rounded capitalize">
                    {style.position}
                  </span>
                  <span className="text-[7px] font-mono text-zinc-600 bg-zinc-800/80 px-1 py-0.5 rounded">
                    {style.font_name}
                  </span>
                </div>

                <p className="text-[8px] text-zinc-600 leading-tight">
                  {style.description}
                </p>
              </button>
            );
          })}

          {styles.length === 0 && (
            <div className="flex flex-col items-center justify-center py-6 text-zinc-600">
              <Loader2 className="h-4 w-4 animate-spin mb-2" />
              <span className="text-[9px]">Loading styles...</span>
            </div>
          )}
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="rounded border border-red-800/40 bg-red-600/10 px-2.5 py-2 text-[9px] text-red-300">
          {error}
        </div>
      )}

      {/* Generate button */}
      <button
        onClick={handleGenerate}
        disabled={generating || !hasClips}
        className={cn(
          "flex items-center justify-center gap-2 rounded-lg px-3 py-2.5 text-[11px] font-semibold transition-all mt-auto",
          generating
            ? "bg-blue-600/20 text-blue-400 border border-blue-500/30 cursor-wait"
            : !hasClips
              ? "bg-zinc-800 text-zinc-600 border border-zinc-700 cursor-not-allowed"
              : "bg-blue-600 text-white hover:bg-blue-500 border border-blue-500 active:scale-[0.98]",
        )}
      >
        {generating ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Generating Captions...
          </>
        ) : (
          <>
            {hasClips ? (
              <Sparkles className="h-3.5 w-3.5" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            {hasClips ? "Generate Captions" : "Add clips first"}
          </>
        )}
      </button>

      {!hasClips && (
        <p className="text-[8px] text-zinc-600 text-center">
          Add media to the timeline, then generate captions.
        </p>
      )}
    </div>
  );
}
