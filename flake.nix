
{
  description = "Minimal Python dev environment (uv-based)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = [ pkgs.uv pkgs.python313 pkgs.ffmpeg ];

        env = {
          UV_PYTHON_DOWNLOADS = "never";
          UV_PYTHON = pkgs.python313.interpreter;
        };

        LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
      };
    };
}
