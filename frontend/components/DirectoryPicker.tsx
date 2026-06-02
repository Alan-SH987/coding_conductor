"use client";

import { useEffect, useState } from "react";
import { api, type DirectoryEntry } from "@/lib/api";
import { Button } from "@/components/ui";

interface DirectoryPickerProps {
  onSelect: (path: string) => void;
  onCancel: () => void;
}

export function DirectoryPicker({ onSelect, onCancel }: DirectoryPickerProps) {
  const [currentPath, setCurrentPath] = useState("");
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [entries, setEntries] = useState<DirectoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [manualPath, setManualPath] = useState("");

  async function loadDirectory(path: string) {
    setLoading(true);
    setError(null);
    try {
      const res = await api.browseDirectory(path);
      setCurrentPath(res.current_path);
      setParentPath(res.parent_path);
      setEntries(res.entries);
      setManualPath(res.current_path);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadDirectory("");
  }, []);

  function handleGoUp() {
    if (parentPath) {
      loadDirectory(parentPath);
    }
  }

  function handleEntryClick(entry: DirectoryEntry) {
    loadDirectory(entry.path);
  }

  function handleSelectCurrent() {
    onSelect(currentPath);
  }

  function handleManualGo() {
    if (manualPath.trim()) {
      loadDirectory(manualPath.trim());
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="flex max-h-[80vh] w-full max-w-lg flex-col rounded-lg border border-border bg-card shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Select Directory</h2>
          <button
            type="button"
            onClick={onCancel}
            className="text-muted-foreground hover:text-foreground"
          >
            ✕
          </button>
        </div>

        <div className="flex items-center gap-2 border-b border-border px-4 py-2">
          <input
            value={manualPath}
            onChange={(e) => setManualPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleManualGo();
            }}
            placeholder="Enter path..."
            className="flex-1 rounded-md border border-border bg-background px-2 py-1 font-mono text-xs outline-none focus:border-ring"
          />
          <Button
            onClick={handleManualGo}
            variant="ghost"
            className="px-2 py-1 text-xs"
          >
            Go
          </Button>
        </div>

        <div className="flex items-center gap-2 border-b border-border bg-muted/30 px-4 py-2">
          <button
            type="button"
            onClick={handleGoUp}
            disabled={!parentPath}
            className="rounded px-2 py-0.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
          >
            ↑ Up
          </button>
          <span className="flex-1 truncate font-mono text-xs text-muted-foreground">
            {currentPath}
          </span>
        </div>

        {error && (
          <div className="border-b border-border bg-red-900/20 px-4 py-2 text-xs text-red-400">
            {error}
          </div>
        )}

        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="px-4 py-8 text-center text-xs text-muted-foreground">
              Loading...
            </div>
          ) : entries.length === 0 ? (
            <div className="px-4 py-8 text-center text-xs text-muted-foreground">
              No subdirectories
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {entries.map((entry) => (
                <li key={entry.path}>
                  <button
                    type="button"
                    onClick={() => handleEntryClick(entry)}
                    className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm hover:bg-muted/50"
                  >
                    <span className="text-muted-foreground">
                      {entry.is_git ? "📁" : "📂"}
                    </span>
                    <span className="flex-1 truncate">{entry.name}</span>
                    {entry.is_git && (
                      <span className="rounded bg-green-900/50 px-1.5 py-0.5 text-[10px] text-green-300">
                        git
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          <Button onClick={onCancel} variant="ghost" className="text-xs">
            Cancel
          </Button>
          <Button onClick={handleSelectCurrent} className="text-xs">
            Select This Directory
          </Button>
        </div>
      </div>
    </div>
  );
}
