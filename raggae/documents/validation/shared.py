SUPPORTED_FORMATS = frozenset(
    {
        "txt",
        "md",
        "csv",
        "html",
        "json",
        "jsonl",
        "pdf",
        "docx",
        "xlsx",
        "pptx",
        "png",
        "jpeg",
        "tiff",
    }
)

EXTENSION_ALIASES = {
    "jpg": "jpeg",
    "tif": "tiff",
}

SUPPORTED_EXTENSIONS = SUPPORTED_FORMATS | EXTENSION_ALIASES.keys()

DANGEROUS_EXTENSIONS = frozenset(
    {
        "exe",
        "dll",
        "com",
        "bat",
        "cmd",
        "msi",
        "scr",
        "ps1",
        "vbs",
        "vbe",
        "js",
        "jse",
        "jar",
        "sh",
        "bash",
        "php",
        "phtml",
        "py",
        "rb",
        "pl",
        "cgi",
        "app",
        "dmg",
        "iso",
    }
)

TEXT_FORMATS = frozenset({"txt", "md", "csv", "html", "json", "jsonl"})
