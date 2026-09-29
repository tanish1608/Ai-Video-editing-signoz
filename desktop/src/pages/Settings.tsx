import { KinetographAPI } from "@/lib/api";
import { useState, useEffect } from "react";
import { ArrowLeft, Eye, EyeOff, Save, RotateCcw } from "lucide-react";
import { cn } from "@/lib/utils";

interface SettingsProps {
  onBack: () => void;
}

interface SettingsState {
  geminiApiKey: string;
  hfToken: string;
  elevenlabsApiKey: string;
  nvidiaApiKey: string;
  pexelsApiKey: string;
  soundstripeApiKey: string;
  geminiModel: string;
  vlmModel: string;
  vlmBaseUrl: string;
  outputWidth: number;
  outputHeight: number;
  outputFps: number;
}

const DEFAULT_SETTINGS: SettingsState = {
  geminiApiKey: "",
  hfToken: "",
  elevenlabsApiKey: "",
  nvidiaApiKey: "",
  pexelsApiKey: "",
  soundstripeApiKey: "",
  geminiModel: "gemini-3.8-flash",
  vlmModel: "nvidia/nemotron-nano-12b-v2-vl",
  vlmBaseUrl: "https://integrate.api.nvidia.com",
  outputWidth: 1080,
  outputHeight: 1920,
  outputFps: 30,
};

const API_KEY_FIELDS: { key: keyof SettingsState; label: string; description: string; required: boolean }[] = [
  { key: "geminiApiKey", label: "Gemini API Key", description: "Google AI Studio — powers the Scripter and music vibe picker", required: true },
  { key: "elevenlabsApiKey", label: "ElevenLabs API Key", description: "Transcription, instrumental music and sound effects", required: true },
  { key: "nvidiaApiKey", label: "NVIDIA API Key", description: "Vision-Language Model for visual scene analysis", required: true },
  { key: "pexelsApiKey", label: "Pexels API Key", description: "Stock footage for B-roll synthesis", required: false },
  { key: "soundstripeApiKey", label: "Soundstripe API Key", description: "Background music search and download", required: false },
  { key: "hfToken", label: "HuggingFace Token", description: "Model downloads (optional)", required: false },
];

const KEY_PROVIDERS: Record<string, string> = {
  geminiApiKey: "gemini", elevenlabsApiKey: "elevenlabs", nvidiaApiKey: "nvidia",
  pexelsApiKey: "pexels", soundstripeApiKey: "soundstripe",
};

export function Settings({ onBack }: SettingsProps) {
  const [settings, setSettings] = useState<SettingsState>(DEFAULT_SETTINGS);
  const [showKeys, setShowKeys] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [keyStatus, setKeyStatus] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");

  useEffect(() => {
    KinetographAPI.getKeyStatus().then((s) => setKeyStatus(s.api_keys))
      .catch(() => setError("Engine unavailable; key presence cannot be checked yet."));
    window.electron?.getSettings().then((s) => {
      if (s && typeof s === "object") {
        setSettings((prev) => ({ ...prev, ...s } as SettingsState));
      }
    }).catch(() => {});
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setError("");
    try {
      if (!window.electron) throw new Error("Save API keys from the desktop app.");
      await window.electron?.saveSettings(settings as unknown as Record<string, unknown>);
      const status = await KinetographAPI.getKeyStatus();
      setKeyStatus(status.api_keys);
      if (settings.geminiApiKey.trim() && !status.api_keys.gemini) {
        throw new Error("Saved, but the engine still cannot see the Gemini key. Check the engine env-file path.");
      }
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save settings.");
    } finally {
      setSaving(false);
    }
  };

  const handleRestartBackend = async () => {
    await handleSave();
    await window.electron?.restartBackend();
  };

  const toggleShowKey = (key: string) => {
    setShowKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const updateField = (key: keyof SettingsState, value: string | number) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
  };

  return (
    <div className="flex h-screen flex-col bg-[#0c0c0e] text-zinc-100">
      {/* Drag region for macOS */}
      <div className="drag-region h-8 w-full shrink-0" />

      {error && <p role="alert" className="px-6 py-2 text-xs text-red-400">{error}</p>}
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-zinc-800 px-6 py-3">
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 rounded-md px-2 py-1 text-sm text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Editor
        </button>
        <h2 className="text-sm font-semibold text-zinc-200">Settings</h2>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={handleRestartBackend}
            className="flex items-center gap-1.5 rounded-md bg-zinc-800 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-700 transition-colors"
          >
            <RotateCcw className="h-3 w-3" />
            Save & Restart Engine
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              saved
                ? "bg-emerald-600/20 text-emerald-400"
                : "bg-blue-600 text-white hover:bg-blue-500",
            )}
          >
            <Save className="h-3 w-3" />
            {saved ? "Saved!" : saving ? "Saving…" : "Save"}
          </button>
        </div>
      </header>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-2xl space-y-8">
          {/* API Keys */}
          <section>
            <h3 className="text-sm font-semibold text-zinc-200 mb-1">API Keys</h3>
            <p className="text-xs text-zinc-500 mb-4">
              These keys are stored locally on your machine and sent directly to the respective services.
            </p>
            <div className="space-y-4">
              {API_KEY_FIELDS.map(({ key, label, description, required }) => (
                <div key={key}>
                  <label className="flex items-center gap-2 text-xs font-medium text-zinc-300 mb-1">
                    {label}
                    {required && <span className="text-red-400 text-[10px]">Required</span>}
                  </label>
                  <p className="text-[10px] text-zinc-600 mb-1.5">{description}</p>
                  {KEY_PROVIDERS[key] && keyStatus[KEY_PROVIDERS[key]] !== undefined && (
                    <p className="text-[10px] text-zinc-400 mb-1.5">
                      {keyStatus[KEY_PROVIDERS[key]] ? "Key present in engine. Leave blank to keep it." : "No key loaded in engine."}
                    </p>
                  )}
                  <div className="flex items-center gap-1">
                    <input
                      type={showKeys.has(key) ? "text" : "password"}
                      value={(settings[key] as string) || ""}
                      onChange={(e) => updateField(key, e.target.value)}
                      placeholder={`Enter ${label}…`}
                      className="flex-1 rounded-md bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
                    />
                    <button
                      onClick={() => toggleShowKey(key)}
                      className="p-2 text-zinc-500 hover:text-zinc-300 transition-colors"
                    >
                      {showKeys.has(key) ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* Model Configuration */}
          <section>
            <h3 className="text-sm font-semibold text-zinc-200 mb-1">Models</h3>
            <p className="text-xs text-zinc-500 mb-4">
              Configure which AI models to use for different pipeline stages.
            </p>
            <div className="space-y-4">
              <div>
                <label className="text-xs font-medium text-zinc-300 mb-1 block">Gemini Model</label>
                <input
                  type="text"
                  value={settings.geminiModel}
                  onChange={(e) => updateField("geminiModel", e.target.value)}
                  className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
                />
              </div>
              <div>
                <label className="text-xs font-medium text-zinc-300 mb-1 block">VLM Model</label>
                <input
                  type="text"
                  value={settings.vlmModel}
                  onChange={(e) => updateField("vlmModel", e.target.value)}
                  className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
                />
              </div>
              <div>
                <label className="text-xs font-medium text-zinc-300 mb-1 block">VLM Base URL</label>
                <input
                  type="text"
                  value={settings.vlmBaseUrl}
                  onChange={(e) => updateField("vlmBaseUrl", e.target.value)}
                  className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
                />
              </div>
            </div>
          </section>

          {/* Output Settings */}
          <section>
            <h3 className="text-sm font-semibold text-zinc-200 mb-1">Output</h3>
            <p className="text-xs text-zinc-500 mb-4">Default render settings.</p>
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="text-xs font-medium text-zinc-300 mb-1 block">Width</label>
                <input
                  type="number"
                  value={settings.outputWidth}
                  onChange={(e) => updateField("outputWidth", parseInt(e.target.value) || 1080)}
                  className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
                />
              </div>
              <div>
                <label className="text-xs font-medium text-zinc-300 mb-1 block">Height</label>
                <input
                  type="number"
                  value={settings.outputHeight}
                  onChange={(e) => updateField("outputHeight", parseInt(e.target.value) || 1920)}
                  className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
                />
              </div>
              <div>
                <label className="text-xs font-medium text-zinc-300 mb-1 block">FPS</label>
                <input
                  type="number"
                  value={settings.outputFps}
                  onChange={(e) => updateField("outputFps", parseInt(e.target.value) || 30)}
                  className="w-full rounded-md bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
                />
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
