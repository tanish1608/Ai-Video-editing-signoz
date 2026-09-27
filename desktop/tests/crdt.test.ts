import { afterEach, expect, test } from "vitest";
import * as Y from "yjs";
import * as crdt from "../src/lib/crdt";

const edit = {
  title: "Project A", total_duration_ms: 1000,
  clips: [{ clip_id: "a", source_file: "a.mp4", in_ms: 0, out_ms: 1000,
    clip_type: "cutaway" as const, description: "A" }],
};

afterEach(() => crdt.resetDoc());

test("reopening a project keeps its saved clips and discards the other project's history", () => {
  crdt.loadPaperEditIntoDoc(edit);
  const saved = Y.encodeStateAsUpdate(crdt.ydoc);
  const firstId = crdt.ydoc.clientID;
  crdt.resetDoc();
  expect(crdt.ydoc.clientID).not.toBe(firstId);
  crdt.loadPaperEditIntoDoc({ ...edit, title: "Project B", clips: [] });
  crdt.resetDoc();
  Y.applyUpdate(crdt.ydoc, saved);
  expect(crdt.paperEditFromDoc()?.title).toBe("Project A");
  expect(crdt.paperEditFromDoc()?.clips).toHaveLength(1);
});

test("observers follow the replacement document and run once per transaction", () => {
  let calls = 0;
  const unsubscribe = crdt.observeDocument(() => calls++);
  crdt.resetDoc();
  calls = 0;
  crdt.loadPaperEditIntoDoc(edit);
  expect(calls).toBe(1);
  unsubscribe();
});

test("undo after switching cannot restore the previous project's clips", () => {
  crdt.loadPaperEditIntoDoc(edit, "user");
  crdt.resetDoc();
  crdt.undoManager.undo();
  expect(crdt.paperEditFromDoc()).toBeNull();
});
