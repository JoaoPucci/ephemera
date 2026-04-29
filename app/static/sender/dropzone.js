// File-upload dropzone wiring for the Image tab. The dropzone is a clickable
// region that also accepts native drag-and-drop; the hidden <input type=file>
// is the actual control under the hood. After a file lands, the preview row
// (filename + size + clear button) reveals; the clear button resets the
// input.
//
// Usage:
//
//   import { bindDropzone } from './dropzone.js';
//
//   bindDropzone({
//     dropzone: document.getElementById('dropzone'),
//     fileInput: document.getElementById('file'),
//     preview: document.getElementById('preview'),
//     fileName: document.getElementById('file-name'),
//     clearFile: document.getElementById('clear-file'),
//   });
//
// All five elements are required -- the helper assumes the template
// rendered the dropzone variant. The sender form's `Image` tab is the
// only consumer today.

export function bindDropzone({ dropzone, fileInput, preview, fileName, clearFile }) {
  function showPreview() {
    if (fileInput.files.length) {
      const f = fileInput.files[0];
      fileName.textContent = `${f.name} (${Math.round(f.size / 1024)} KB)`;
      preview.hidden = false;
    } else {
      preview.hidden = true;
    }
  }

  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fileInput.click();
    }
  });
  dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('drag');
  });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('drag');
    if (e.dataTransfer.files.length) {
      fileInput.files = e.dataTransfer.files;
      showPreview();
    }
  });
  fileInput.addEventListener('change', showPreview);
  clearFile.addEventListener('click', (e) => {
    e.stopPropagation();
    fileInput.value = '';
    preview.hidden = true;
  });
}
