"""Render the real Typst templates without sharing mutable render inputs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from nannanews import runtime

PACKAGES = (("tiaoma", "0.3.0"), ("wrap-it", "0.1.1"))


def typst_command():
    """Portable command prefix shared by PDF and Main's adaptive PNG renderer."""
    binary = runtime.settings.typst_binary
    if os.sep in binary:
        executable = Path(binary).expanduser()
        if not executable.is_absolute():
            executable = runtime.CODE_DIR / executable
        binary = str(executable.resolve())
    command = [binary, "compile"]
    if runtime.settings.typst_package_cache:
        command += ["--package-cache-path", str(runtime.settings.typst_package_cache)]
    if runtime.settings.typst_font_dir:
        command += ["--font-path", str(runtime.settings.typst_font_dir)]
    return command


def _data_path(value):
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = runtime.path(candidate)
    candidate = candidate.resolve()
    if not candidate.is_relative_to(runtime.settings.data_dir):
        raise ValueError("Publication input/output must be inside NANNA_DATA_DIR")
    return candidate


@contextmanager
def render_workspace(json_path, typ_path):
    """Yield (workspace, template) for isolated PDF or PNG rendering.

    Relative JSON paths refer to the private data directory; relative template paths
    refer to the code directory. Only the two reviewed templates may be compiled.
    Images are copied only when referenced, and must resolve beneath upload_dir.
    """
    source = _data_path(json_path)
    template = Path(typ_path)
    if not template.is_absolute():
        template = runtime.CODE_DIR / template
    template = template.resolve()
    if template not in (runtime.CODE_DIR / "news_template.typ", runtime.CODE_DIR / "lecture_template.typ"):
        raise ValueError("Only the existing reviewed publication templates are supported")
    issue = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(issue, dict) or not isinstance(issue.get("data"), dict) or not isinstance(issue.get("due"), dict):
        raise ValueError("Publication JSON must contain data and due objects")
    issue["data"].update(
        site_name=runtime.settings.site_name, public_url=runtime.settings.public_url,
        subscription_url=runtime.settings.qq_group_url,
        subscription_label=runtime.settings.qq_group_label,
    )
    with tempfile.TemporaryDirectory(prefix="nanna-render-") as temporary:
        workspace = Path(temporary)
        staged_template = workspace / template.name
        shutil.copyfile(template, staged_template)
        (workspace / "issue.json").write_text(json.dumps(issue, ensure_ascii=False), encoding="utf-8")
        image_dir = workspace / "static" / "uploads"
        for section in ("college", "club", "career", "lecture", "other", "reference"):
            rows = issue["data"].get(section, [])
            if not isinstance(rows, list):
                raise ValueError(f"Publication section {section} must be an array")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Publication items must be objects")
                image = row.get("image")
                if image is None:
                    continue
                if not isinstance(image, str) or not image:
                    raise ValueError("Invalid publication image name")
                relative = Path(image)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Publication image escapes upload directory")
                original = (runtime.settings.upload_dir / relative).resolve()
                if not original.is_relative_to(runtime.settings.upload_dir) or not original.is_file():
                    raise ValueError("Publication image is missing or outside upload directory")
                destination = image_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(original, destination)
        yield workspace, staged_template


def atomic_output(source, destination):
    """Replace a private publication artifact only after a completed render."""
    destination = _data_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".render-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream, Path(source).open("rb") as rendered:
            shutil.copyfileobj(rendered, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def compile_pdf(json_path, typ_path, pdf_path, timeout=180):
    """Return the real compiler result; never replace output on failure/timeout."""
    destination = _data_path(pdf_path)
    if destination.suffix.lower() != ".pdf":
        raise ValueError("PDF output must have a .pdf extension")
    with render_workspace(json_path, typ_path) as (workspace, template):
        output = workspace / "output.pdf"
        command = typst_command() + ["--root", str(workspace), "--input", "issue=issue.json",
                                     "--pdf-standard", "2.0", str(template), str(output)]
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("Typst returned success without a PDF artifact")
            atomic_output(output, destination)
        return result


def compile_information_pdf(records, filters, pdf_path, timeout=180):
    """Export an explicit query snapshot; never read or mutate publication state."""
    if not isinstance(records, list) or not records or len(records) > 500:
        raise ValueError('Query export needs 1–500 matching records')
    if not isinstance(filters, dict):
        raise ValueError('Query export filters must be an object')
    destination = _data_path(pdf_path)
    if destination.suffix.lower() != '.pdf':
        raise ValueError('PDF output must have a .pdf extension')
    # Only the snapshot needed by the template crosses the rendering boundary.
    fields = ('title', 'description', 'source_name', 'source_url',
              'source_published_date', 'event_date', 'deadline_date',
              'publication_date', 'verification_status', 'model_status',
              'date_uncertainty', 'colleges')
    snapshot = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('title'), str):
            raise ValueError('Query export contains an invalid information record')
        snapshot.append({key: record.get(key) for key in fields})
    payload = {'site_name': runtime.settings.site_name, 'filters': filters,
               'records': snapshot, 'private_export': True}
    with tempfile.TemporaryDirectory(prefix='nanna-query-export-') as temporary:
        workspace = Path(temporary)
        template = workspace / 'information_export.typ'
        shutil.copyfile(runtime.CODE_DIR / template.name, template)
        (workspace / 'records.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        output = workspace / 'output.pdf'
        command = typst_command() + ['--root', str(workspace), '--input', 'records=records.json',
                                     '--pdf-standard', '2.0', str(template), str(output)]
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError('Typst returned success without a PDF artifact')
            atomic_output(output, destination)
        return result
