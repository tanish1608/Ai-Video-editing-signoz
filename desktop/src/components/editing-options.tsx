import { useEffect, useState } from "react";
import { KinetographAPI, type EditingOptions } from "@/lib/api";
import { useChatStore } from "@/store/use-chat-store";

export function EditingOptionsPanel() {
  const [options, setOptions] = useState<EditingOptions | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const busy = useChatStore((s) => s.pipelineActive);
  useEffect(() => {
    let active = true;
    KinetographAPI.getEditingOptions().then((value) => {
      if (active) setOptions(value);
    }).catch(() => { if (active) setError("Could not load project editing options."); });
    return () => { active = false; };
  }, []);
  async function update(patch: Partial<EditingOptions>) {
    if (!options) return;
    setSaving(true);
    setError("");
    try {
      // Refresh first so another panel's caption preference is preserved.
      const current = await KinetographAPI.getEditingOptions();
      setOptions(await KinetographAPI.saveEditingOptions({ ...current, ...patch }));
    } catch {
      setError("Could not save options. Wait for the current edit and try again.");
    } finally { setSaving(false); }
  }
  const disabled = busy || saving || !options;
  return (
    <fieldset disabled={disabled} className="mb-2 space-y-2 text-[10px] text-zinc-400">
      <div className="flex flex-wrap items-center gap-2">
        <label>Project style{" "}
          <select aria-label="Project editing style" value={options?.editing_mode ?? "narration"}
            onChange={(e) => update({ editing_mode: e.target.value as EditingOptions["editing_mode"] })}
            className="rounded bg-zinc-900 border border-zinc-700 p-1 text-zinc-200">
            <option value="narration">Narration / social</option>
            <option value="highlights">Cinematic highlights</option>
          </select>
        </label>
        <label>Music{" "}
          <select aria-label="Music source" value={options?.audio_provider ?? "elevenlabs"}
            onChange={(e) => update({ audio_provider: e.target.value as EditingOptions["audio_provider"] })}
            className="rounded bg-zinc-900 border border-zinc-700 p-1 text-zinc-200">
            <option value="elevenlabs">ElevenLabs</option>
            <option value="soundstripe">Soundstripe</option>
            <option value="none">None</option>
          </select>
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={options?.sound_effects_enabled ?? true}
            onChange={(e) => update({ sound_effects_enabled: e.target.checked })} />
          Occasional sound effects
        </label>
      </div>
      <p>Saved for this project. Style changes guide the next script; music changes apply on the next audio edit.</p>
      {error && <p role="alert" className="text-red-400">{error}</p>}
    </fieldset>
  );
}
