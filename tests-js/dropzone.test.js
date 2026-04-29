import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadModule } from './helpers.js';

// ---------------------------------------------------------------------------
// bindDropzone wires up four flows: click-to-browse, keyboard activation,
// native HTML5 drag-and-drop, and the explicit clear button. Each flow ends
// with the preview row showing or hiding based on whether files are
// present in the hidden <input type=file>. The existing sender.test.js
// suite doesn't dispatch drag events, so the handler bodies were
// previously uncovered when this code lived inline in form.js. The split
// surfaced the gap as a per-file ratio; this suite closes it.
// ---------------------------------------------------------------------------

function mountDropzoneFixture() {
  document.body.innerHTML = `
    <div id="dropzone" tabindex="0">
      <input type="file" id="file" hidden>
      <div id="preview" hidden>
        <span id="file-name"></span>
        <button type="button" id="clear-file">clear</button>
      </div>
    </div>
  `;
  return {
    dropzone: document.getElementById('dropzone'),
    fileInput: document.getElementById('file'),
    preview: document.getElementById('preview'),
    fileName: document.getElementById('file-name'),
    clearFile: document.getElementById('clear-file'),
  };
}

// Build a File-like object that the dropzone handlers can introspect.
// jsdom doesn't ship `DataTransfer`, so to populate `fileInput.files`
// we override the property descriptor with an array of Files (the
// handler reads `.length` and `[0]`, both of which work on a plain
// array, so this is enough).
function makeFile(name = 'photo.png', sizeBytes = 12_345) {
  const blob = new Blob([new Uint8Array(sizeBytes)], { type: 'image/png' });
  return new File([blob], name, { type: 'image/png' });
}

function setFiles(input, files) {
  // writable so the production handler's `fileInput.files =
  // e.dataTransfer.files` assignment lands cleanly; configurable so a
  // subsequent test can re-stub fresh.
  Object.defineProperty(input, 'files', {
    configurable: true,
    writable: true,
    value: files,
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe('dropzone.js — click + keyboard activation', () => {
  it('clicking the dropzone forwards to fileInput.click()', async () => {
    const els = mountDropzoneFixture();
    // mockImplementation prevents the spy from calling through to the
    // real HTMLInputElement.click(), which would dispatch a click event
    // on fileInput that bubbles up to dropzone and re-fires the same
    // handler -- a quirk of jsdom's click bubbling that real browsers
    // sidestep via the native file-picker dialog grabbing focus.
    const inputClick = vi.spyOn(els.fileInput, 'click').mockImplementation(() => {});
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    els.dropzone.click();

    expect(inputClick).toHaveBeenCalledOnce();
  });

  it('Enter on the focused dropzone triggers fileInput.click()', async () => {
    const els = mountDropzoneFixture();
    // mockImplementation prevents the spy from calling through to the
    // real HTMLInputElement.click(), which would dispatch a click event
    // on fileInput that bubbles up to dropzone and re-fires the same
    // handler -- a quirk of jsdom's click bubbling that real browsers
    // sidestep via the native file-picker dialog grabbing focus.
    const inputClick = vi.spyOn(els.fileInput, 'click').mockImplementation(() => {});
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    const ev = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true });
    els.dropzone.dispatchEvent(ev);

    expect(inputClick).toHaveBeenCalledOnce();
    expect(ev.defaultPrevented).toBe(true);
  });

  it('Space on the focused dropzone triggers fileInput.click()', async () => {
    const els = mountDropzoneFixture();
    // mockImplementation prevents the spy from calling through to the
    // real HTMLInputElement.click(), which would dispatch a click event
    // on fileInput that bubbles up to dropzone and re-fires the same
    // handler -- a quirk of jsdom's click bubbling that real browsers
    // sidestep via the native file-picker dialog grabbing focus.
    const inputClick = vi.spyOn(els.fileInput, 'click').mockImplementation(() => {});
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    const ev = new KeyboardEvent('keydown', { key: ' ', cancelable: true });
    els.dropzone.dispatchEvent(ev);

    expect(inputClick).toHaveBeenCalledOnce();
    expect(ev.defaultPrevented).toBe(true);
  });

  it('other keys (e.g. Tab) do not click fileInput', async () => {
    const els = mountDropzoneFixture();
    // mockImplementation prevents the spy from calling through to the
    // real HTMLInputElement.click(), which would dispatch a click event
    // on fileInput that bubbles up to dropzone and re-fires the same
    // handler -- a quirk of jsdom's click bubbling that real browsers
    // sidestep via the native file-picker dialog grabbing focus.
    const inputClick = vi.spyOn(els.fileInput, 'click').mockImplementation(() => {});
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    const ev = new KeyboardEvent('keydown', { key: 'Tab', cancelable: true });
    els.dropzone.dispatchEvent(ev);

    expect(inputClick).not.toHaveBeenCalled();
    expect(ev.defaultPrevented).toBe(false);
  });
});

describe('dropzone.js — drag visual feedback', () => {
  it('dragover adds the .drag class and prevents default (so drop fires)', async () => {
    const els = mountDropzoneFixture();
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    const ev = new Event('dragover', { cancelable: true });
    els.dropzone.dispatchEvent(ev);

    expect(els.dropzone.classList.contains('drag')).toBe(true);
    expect(ev.defaultPrevented).toBe(true);
  });

  it('dragleave removes the .drag class', async () => {
    const els = mountDropzoneFixture();
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    els.dropzone.classList.add('drag');
    els.dropzone.dispatchEvent(new Event('dragleave'));

    expect(els.dropzone.classList.contains('drag')).toBe(false);
  });
});

describe('dropzone.js — drop event populates fileInput and shows preview', () => {
  it('a drop with files copies them to fileInput, removes .drag, shows preview', async () => {
    const els = mountDropzoneFixture();
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    els.dropzone.classList.add('drag');

    // The handler does `fileInput.files = e.dataTransfer.files`. The
    // assignment runs through fileInput's setter (or no-ops in jsdom);
    // either way, we follow up with a stubbed `.files` so the
    // showPreview() readback finds what the handler intended to set.
    const droppedFile = makeFile('drag-drop.png', 2048);
    const ev = new Event('drop', { cancelable: true });
    Object.defineProperty(ev, 'dataTransfer', {
      value: { files: [droppedFile] },
    });
    // Pre-stub the input so the handler's `fileInput.files = ...`
    // assignment lands on a configurable property and showPreview
    // reads back our test-supplied list.
    setFiles(els.fileInput, [droppedFile]);
    els.dropzone.dispatchEvent(ev);

    expect(ev.defaultPrevented).toBe(true);
    expect(els.dropzone.classList.contains('drag')).toBe(false);
    expect(els.preview.hidden).toBe(false);
    expect(els.fileName.textContent).toContain('drag-drop.png');
  });

  it('drop with no files is a no-op (preview stays hidden, .drag still cleared)', async () => {
    const els = mountDropzoneFixture();
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    els.dropzone.classList.add('drag');

    const ev = new Event('drop', { cancelable: true });
    Object.defineProperty(ev, 'dataTransfer', { value: { files: [] } });
    els.dropzone.dispatchEvent(ev);

    expect(els.dropzone.classList.contains('drag')).toBe(false);
    expect(els.preview.hidden).toBe(true);
  });
});

describe('dropzone.js — fileInput change + clear button', () => {
  it('change event with a file selected reveals the preview row with name + KB size', async () => {
    const els = mountDropzoneFixture();
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    setFiles(els.fileInput, [makeFile('selected.jpg', 4096)]); // 4 KB
    els.fileInput.dispatchEvent(new Event('change'));

    expect(els.preview.hidden).toBe(false);
    expect(els.fileName.textContent).toContain('selected.jpg');
    // Math.round(4096/1024) = 4 -- the helper renders KB.
    expect(els.fileName.textContent).toContain('4 KB');
  });

  it('change event with no files (programmatic reset) hides the preview', async () => {
    const els = mountDropzoneFixture();
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    els.preview.hidden = false;
    // No files -- equivalent to the input being cleared by the user / form.reset().
    els.fileInput.dispatchEvent(new Event('change'));

    expect(els.preview.hidden).toBe(true);
  });

  it('clearFile click resets fileInput.value, hides preview, and stops bubbling to the dropzone', async () => {
    const els = mountDropzoneFixture();
    const dropzoneClick = vi.fn();
    els.dropzone.addEventListener('click', dropzoneClick);
    const { bindDropzone } = await loadModule('sender/dropzone');

    bindDropzone(els);
    // Pretend a file was selected and the preview is showing.
    setFiles(els.fileInput, [makeFile()]);
    els.preview.hidden = false;

    els.clearFile.click();

    expect(els.fileInput.value).toBe('');
    expect(els.preview.hidden).toBe(true);
    // stopPropagation prevents the dropzone's click-to-browse handler
    // from re-opening the file dialog when the user clears the selection.
    expect(dropzoneClick).not.toHaveBeenCalled();
  });
});
