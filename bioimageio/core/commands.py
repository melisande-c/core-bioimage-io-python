"""The `Bioimageio` class defined here has static methods that constitute the `bioimageio` command line interface (using fire)"""

import difflib
import sys
from pathlib import Path
from typing import Dict, List, Optional, Union

import fire
from tqdm import tqdm

import bioimageio.spec.generic
from bioimageio.core import __version__, test_description
from bioimageio.core._prediction_pipeline import create_prediction_pipeline
from bioimageio.core.common import MemberId
from bioimageio.core.digest_spec import load_sample_for_model
from bioimageio.core.io import save_sample
from bioimageio.core.stat_measures import Stat
from bioimageio.spec import (
    InvalidDescr,
    load_description,
    load_description_and_validate_format_only,
    save_bioimageio_package,
    save_bioimageio_package_as_folder,
)
from bioimageio.spec.dataset import DatasetDescr
from bioimageio.spec.model import ModelDescr
from bioimageio.spec.model.v0_5 import WeightsFormat
from bioimageio.spec.notebook import NotebookDescr


class Bioimageio:
    """🦒 CLI to work with resources shared on bioimage.io"""

    @staticmethod
    def package(
        source: str,
        path: str = "bioimageio-package.zip",
        weight_format: Optional[WeightsFormat] = None,
    ):
        """Package a bioimageio resource as a zip file.

        Note: If `path` does not have a `.zip` suffix this command will save the
              package as an unzipped folder instead.

        Args:
            source: RDF source e.g. `bioimageio.yaml` or `http://example.com/rdf.yaml`
            path: output path
            weight-format: include only this single weight-format
        """
        output_path = Path(path)
        if output_path.suffix == ".zip":
            _ = save_bioimageio_package(
                source,
                output_path=output_path,
                weights_priority_order=(
                    None if weight_format is None else (weight_format,)
                ),
            )
        else:
            _ = save_bioimageio_package_as_folder(
                source,
                output_path=output_path,
                weights_priority_order=(
                    None if weight_format is None else (weight_format,)
                ),
            )

    @staticmethod
    def test(
        source: str,
        weight_format: Optional[WeightsFormat] = None,
        *,
        devices: Optional[Union[str, List[str]]] = None,
        decimal: int = 4,
    ):
        """test a bioimageio resource

        Args:
            source: Path or URL to the bioimageio resource description file
                    (bioimageio.yaml or rdf.yaml) or to a zipped resource
            weight_format: (model only) The weight format to use
            devices: Device(s) to use for testing
            decimal: Precision for numerical comparisons
        """
        print(f"\ntesting {source}...")
        summary = test_description(
            source,
            weight_format=None if weight_format is None else weight_format,
            devices=[devices] if isinstance(devices, str) else devices,
            decimal=decimal,
        )
        summary.display()
        sys.exit(0 if summary.status == "passed" else 1)

    @staticmethod
    def validate_format(
        source: str,
    ):
        """validate the meta data format of a bioimageio resource description

        Args:
            source: Path or URL to the bioimageio resource description file
                    (bioimageio.yaml or rdf.yaml) or to a zipped resource
        """
        print(f"\validating meta data format of {source}...")
        summary = load_description_and_validate_format_only(source)
        summary.display()
        sys.exit(0 if summary.status == "passed" else 1)

    @staticmethod
    def predict(
        model: str,
        output_pattern: str = "{detected_sample_name}_{i:04}/{member_id}.npy",
        overwrite: bool = False,
        with_blocking: bool = False,
        # precomputed_stats: Path,  # TODO: add arg to read precomputed stats as yaml or json
        **inputs: str,
    ):
        if "{member_id}" not in output_pattern:
            raise ValueError("'{member_id}' must be included in output_pattern")

        glob_matched_inputs: Dict[MemberId, List[Path]] = {}
        n_glob_matches: Dict[int, List[str]] = {}
        seq_matcher: Optional[difflib.SequenceMatcher[str]] = None
        detected_sample_name = "sample"
        for name, pattern in inputs.items():
            paths = sorted(Path().glob(pattern))
            if not paths:
                raise FileNotFoundError(f"No file matched glob pattern '{pattern}'")

            glob_matched_inputs[MemberId(name)] = paths
            n_glob_matches.setdefault(len(paths), []).append(pattern)
            if seq_matcher is None:
                seq_matcher = difflib.SequenceMatcher(a=paths[0].name)
            else:
                seq_matcher.set_seq2(paths[0].name)
                detected_sample_name = "_".join(
                    paths[0].name[m.b : m.b + m.size]
                    for m in seq_matcher.get_matching_blocks()
                    if m.size > 3
                )

        if len(n_glob_matches) > 1:
            raise ValueError(
                f"Different match counts for input glob patterns: '{n_glob_matches}'"
            )
        n_inputs = list(n_glob_matches)[0]
        if n_inputs == 0:
            raise FileNotFoundError(
                f"Did not find any input files at {inputs} respectively"
            )

        if n_inputs > 1 and "{i}" not in output_pattern and "{i:" not in output_pattern:
            raise ValueError(
                f"Found multiple input samples, thus `output_pattern` ({output_pattern})"
                + " must include a replacement field for `i` delimited by {}, e.g. {i}."
                + " See https://docs.python.org/3/library/string.html#formatstrings for formatting details."
            )

        model_descr = load_description(model)
        model_descr.validation_summary.display()
        if isinstance(model_descr, InvalidDescr):
            raise ValueError("model is invalid")

        if model_descr.type != "model":
            raise ValueError(
                f"expected a model resource, but got resource type '{model_descr.type}'"
            )

        assert not isinstance(
            model_descr,
            (
                bioimageio.spec.generic.v0_2.GenericDescr,
                bioimageio.spec.generic.v0_3.GenericDescr,
            ),
        )

        pp = create_prediction_pipeline(model_descr)
        predict_method = (
            pp.predict_sample_with_blocking
            if with_blocking
            else pp.predict_sample_without_blocking
        )
        stat: Stat = {}
        for i in tqdm(range(n_inputs), total=n_inputs, desc="predict"):
            output_path = Path(
                output_pattern.format(
                    detected_sample_name=detected_sample_name,
                    i=i,
                    member_id="{member_id}",
                )
            )
            if not overwrite and output_path.exists():
                raise FileExistsError(output_path)

            input_sample = load_sample_for_model(
                model=model_descr,
                paths={name: paths[i] for name, paths in glob_matched_inputs.items()},
                stat=stat,
                sample_id=f"{detected_sample_name}_{i}",
            )
            output_sample = predict_method(input_sample)
            save_sample(output_path, output_sample)


assert isinstance(Bioimageio.__doc__, str)
Bioimageio.__doc__ += f"""

library versions:
  bioimageio.core {__version__}
  bioimageio.spec {__version__}

spec format versions:
        model RDF {ModelDescr.implemented_format_version}
      dataset RDF {DatasetDescr.implemented_format_version}
     notebook RDF {NotebookDescr.implemented_format_version}

"""

# if torch_converter is not None:

#     @app.command()
#     def convert_torch_weights_to_onnx(
#         model_rdf: Path = typer.Argument(
#             ..., help="Path to the model resource description file (rdf.yaml) or zipped model."
#         ),
#         output_path: Path = typer.Argument(..., help="Where to save the onnx weights."),
#         opset_version: Optional[int] = typer.Argument(12, help="Onnx opset version."),
#         use_tracing: bool = typer.Option(True, help="Whether to use torch.jit tracing or scripting."),
#         verbose: bool = typer.Option(True, help="Verbosity"),
#     ):
#         ret_code = torch_converter.convert_weights_to_onnx(model_rdf, output_path, opset_version, use_tracing, verbose)
#         sys.exit(ret_code)

#     convert_torch_weights_to_onnx.__doc__ = torch_converter.convert_weights_to_onnx.__doc__

#     @app.command()
#     def convert_torch_weights_to_torchscript(
#         model_rdf: Path = typer.Argument(
#             ..., help="Path to the model resource description file (rdf.yaml) or zipped model."
#         ),
#         output_path: Path = typer.Argument(..., help="Where to save the torchscript weights."),
#         use_tracing: bool = typer.Option(True, help="Whether to use torch.jit tracing or scripting."),
#     ):
#         torch_converter.convert_weights_to_torchscript(model_rdf, output_path, use_tracing)
#         sys.exit(0)

#     convert_torch_weights_to_torchscript.__doc__ = torch_converter.convert_weights_to_torchscript.__doc__


# if keras_converter is not None:

#     @app.command()
#     def convert_keras_weights_to_tensorflow(
#         model_rdf: Annotated[
#             Path, typer.Argument(help="Path to the model resource description file (rdf.yaml) or zipped model.")
#         ],
#         output_path: Annotated[Path, typer.Argument(help="Where to save the tensorflow weights.")],
#     ):
#         rd = load_description(model_rdf)
#         ret_code = keras_converter.convert_weights_to_tensorflow_saved_model_bundle(rd, output_path)
#         sys.exit(ret_code)

#     convert_keras_weights_to_tensorflow.__doc__ = (
#         keras_converter.convert_weights_to_tensorflow_saved_model_bundle.__doc__
#     )


def main():
    fire.Fire(Bioimageio, name="bioimageio")


if __name__ == "__main__":
    main()
