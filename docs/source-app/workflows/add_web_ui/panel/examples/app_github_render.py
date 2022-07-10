import io
import os
import subprocess
import sys
from copy import deepcopy
from functools import partial
from subprocess import Popen
from typing import Dict, List, Optional

from lightning import BuildConfig, CloudCompute, LightningApp, LightningFlow
from lightning.app import structures
from lightning.app.components.python import TracerPythonScript
from panel_frontend import PanelFrontend
from lightning.app.storage.path import Path
from app_state_watcher import AppStateWatcher

class GithubRepoRunner(TracerPythonScript):
    def __init__(
        self,
        id: str,
        github_repo: str,
        script_path: str,
        script_args: List[str],
        requirements: List[str],
        cloud_compute: Optional[CloudCompute] = None,
        **kwargs,
    ):
        """The GithubRepoRunner Component clones a repo,
        runs a specific script with provided arguments and collect logs.

        Arguments:
            id: Identified of the component.
            github_repo: The Github Repo URL to clone.
            script_path: The path to the script to execute.
            script_args: The arguments to be provided to the script.
            requirements: The python requirements tp run the script.
            cloud_compute: The object to select the cloud instance.
        """
        super().__init__(
            script_path=__file__,
            script_args=script_args,
            cloud_compute=cloud_compute,
            cloud_build_config=BuildConfig(requirements=requirements),
        )
        self.script_path=script_path
        self.id = id
        self.github_repo = github_repo
        self.kwargs = kwargs
        self.logs = []

    def run(self, *args, **kwargs):
        # 1. Hack: Patch stdout so we can capture the logs.
        string_io = io.StringIO()
        sys.stdout = string_io

        # 2: Use git command line to clone the repo.
        repo_name = self.github_repo.split("/")[-1].replace(".git", "")
        cwd = os.path.dirname(__file__)
        subprocess.Popen(
            f"git clone {self.github_repo}", cwd=cwd, shell=True).wait()

        # 3: Execute the parent run method of the TracerPythonScript class.
        os.chdir(os.path.join(cwd, repo_name))
        super().run(*args, **kwargs)

        # 4: Get all the collected logs and add them to the state.
        # This isn't optimal as heavy, but works for this demo purpose.
        self.logs = string_io.getvalue()
        string_io.close()

    def configure_layout(self):
        return {"name": self.id, "content": self}


class PyTorchLightningGithubRepoRunner(GithubRepoRunner):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.best_model_path = None
        self.best_model_score = None

    def configure_tracer(self):
        from pytorch_lightning import Trainer
        from pytorch_lightning.callbacks import Callback

        tracer = super().configure_tracer()

        class TensorboardServerLauncher(Callback):
            def __init__(self, work):
                # The provided `work` is the
                # current ``PyTorchLightningScript`` work.
                self.w = work

            def on_train_start(self, trainer, *_):
                # Add `host` and `port` for tensorboard to work in the cloud.
                cmd = f"tensorboard --logdir='{trainer.logger.log_dir}'"
                server_args = f"--host {self.w.host} --port {self.w.port}"
                Popen(cmd + " " + server_args, shell=True)

        def trainer_pre_fn(self, *args, work=None, **kwargs):
            # Intercept Trainer __init__ call
            # and inject a ``TensorboardServerLauncher`` component.
            kwargs["callbacks"].append(TensorboardServerLauncher(work))
            return {}, args, kwargs

        # 5. Patch the `__init__` method of the Trainer
        # to inject our callback with a reference to the work.
        tracer.add_traced(
            Trainer, "__init__", pre_fn=partial(trainer_pre_fn, work=self))
        return tracer

    def on_after_run(self, end_script_globals):
        import torch
        # 1. Once the script has finished to execute,
        # we can collect its globals and access any objects.
        trainer = end_script_globals["cli"].trainer
        checkpoint_callback = trainer.checkpoint_callback
        lightning_module = trainer.lightning_module

        # 2. From the checkpoint_callback,
        # we are accessing the best model weights
        checkpoint = torch.load(checkpoint_callback.best_model_path)

        # 3. Load the best weights and torchscript the model.
        lightning_module.load_state_dict(checkpoint["state_dict"])
        lightning_module.to_torchscript(f"{self.name}.pt")

        # 4. Use lightning.app.storage.Pathto create a reference to the
        # torch scripted model. In the cloud with multiple machines,
        # by simply passing this reference to another work,
        # it triggers automatically a file transfer.
        self.best_model_path = Path(f"{self.name}.pt")

        # 5. Keep track of the metrics.
        self.best_model_score = float(checkpoint_callback.best_model_score)


class KerasGithubRepoRunner(GithubRepoRunner):
    """Left to the users to implement"""

class TensorflowGithubRepoRunner(GithubRepoRunner):
    """Left to the users to implement"""

GITHUB_REPO_RUNNERS = {
    "PyTorch Lightning": PyTorchLightningGithubRepoRunner,
    "Keras": KerasGithubRepoRunner,
    "Tensorflow": TensorflowGithubRepoRunner,
}


class Flow(LightningFlow):
    def __init__(self):
        super().__init__()
        # 1: Keep track of the requests within the state
        self.requests = []
        # 2: Create a dictionary of components.
        self.ws = structures.Dict()

    def run(self):
        # Iterate continuously over all requests
        for request_id, request in enumerate(self.requests):
            self._handle_request(request_id, deepcopy(request))

    def _handle_request(self, request_id: int, request: Dict):
        # 1: Create a name and find selected framework
        name = f"w_{request_id}"
        ml_framework = request["train"].pop("ml_framework")

        # 2: If the component hasn't been created yet, create it.
        if name not in self.ws:
            work_cls = GITHUB_REPO_RUNNERS[ml_framework]
            work = work_cls(id=request["id"], **request["train"])
            self.ws[name] = work

        # 3: Run the component
        self.ws[name].run()

        # 4: Once the component has finished, add metadata to the request.
        if self.ws[name].best_model_path:
            request = self.requests[request_id]
            request["best_model_score"] = self.ws[name].best_model_score
            request["best_model_path"] = self.ws[name].best_model_path

    def configure_layout(self):
        # Create a StreamLit UI for the user to run his Github Repo.
        return PanelFrontend("panel_github_render.py")


class RootFlow(LightningFlow):
    def __init__(self):
        super().__init__()
        self.flow = Flow()

    def run(self):
        self.flow.run()

    def configure_layout(self):
        selection_tab = [{"name": "Run your Github Repo", "content": self.flow}]
        run_tabs = [e.configure_layout() for e in self.flow.ws.values()]
        return selection_tab + run_tabs


app = LightningApp(RootFlow())