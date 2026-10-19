{
  lib,
  python3Packages,
  gitMinimal,
  makeWrapper,
}:
python3Packages.buildPythonApplication {
  pname = "infrastructure-memory";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSourceWith {
    src = ./.;
    filter =
      path: type:
      lib.cleanSourceFilter path type
      && !(lib.elem (builtins.baseNameOf path) [
        ".venv"
        ".direnv"
        ".ruff_cache"
        "__pycache__"
        "build"
        "dist"
        "result"
      ]);
  };
  build-system = [ python3Packages.setuptools ];
  dependencies = with python3Packages; [
    cryptography
    numpy
    mcp
    onnxruntime
    tokenizers
    huggingface-hub
  ];
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ gitMinimal ];
  checkPhase = /* bash */ ''
    runHook preCheck
    export PYTHONPATH="$PWD/src"
    python -B -m unittest discover -s tests -v
    runHook postCheck
  '';
  postFixup = /* bash */ ''
    wrapProgram $out/bin/infra-memory --prefix PATH : ${lib.makeBinPath [ gitMinimal ]}
  '';
  pythonImportsCheck = [ "infrastructure_memory" ];
  meta = {
    description = "Local infrastructure memory with Syncthing-portable records";
    mainProgram = "infra-memory";
    platforms = lib.platforms.linux;
  };
}
