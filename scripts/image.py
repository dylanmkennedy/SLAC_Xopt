import json
from copy import copy
from time import sleep
import time
from typing import Union

import h5py
import numpy as np
import pandas as pd
import yaml
from epics import caget_many, caput, caget
from matplotlib import pyplot as plt, patches
from pydantic import BaseModel, PositiveFloat, PositiveInt

from scripts.utils.fitting_methods import fit_gaussian_linear_background


class ROI(BaseModel):
    xmin: int
    xmax: int
    ymin: int
    ymax: int

    @property
    def bounding_box(self):
        return [self.xmin, self.xmax, self.xmax-self.xmin, self.ymax-self.ymin]

    def crop_image(self, img):
        x_size, y_size = img.shape

        if self.xmax > x_size or self.ymax > y_size:
            raise ValueError(f"must specify ROI that is smaller than the image, "
                             f"image size is {img.shape}")

        img = img[self.xmin:self.xmax, self.ymin:self.ymax]

        return img


class ImageDiagnostic(BaseModel):
    screen_name: str
    array_data_suffix: str = "Image:ArrayData"
    array_n_cols_suffix: str = "Image:ArraySize0_RBV"
    array_n_rows_suffix: str = "Image:ArraySize1_RBV"
    resolution_suffix: str = "RESOLUTION"
    beam_shutter_pv: str = None

    background_file: str = None
    save_image_location: Union[str, None] = None
    roi: ROI = None

    min_log_intensity: float = 4.0
    bounding_box_half_width: PositiveFloat = 3.0
    wait_time: PositiveFloat = 1.0
    n_fitting_restarts: PositiveInt = 1
    visualize: bool = True

    testing: bool = False

    def measure_beamsize(self, n_shots: int = 1, **kwargs):
        """
        conduct a multi-shot measurement to get the beam size from images, returns
        sizes in units of `resolution`

        allows attaching extra information to dataset via kwargs
        """
        results = []
        images = []
        start_time = time.time()
        for _ in range(n_shots):
            img, resolution = self.get_processed_image()
            result = self.calculate_beamsize(img)

            # convert beam size results to microns
            if result["Sx"] is not None:
                result['Sx'] = result['Sx'] * resolution
                result['Sy'] = result['Sy'] * resolution

            results += [result]
            images += [img]
            sleep(self.wait_time)

        # combine data into a single dictionary output
        if n_shots == 1:
            outputs = results[0]
        else:
            # collect results into lists
            outputs = pd.DataFrame(results).reset_index().to_dict(orient='list')
            outputs.pop("index")

            # create numpy arrays from lists
            outputs = {key: list(np.array(ele)) for key, ele in outputs.items()}

        # if specified, save image data to location based on time stamp
        if self.save_image_location is not None:
            screen_name = self.screen_name.replace(":", "_")
            save_filename = f"{self.save_image_location}/{screen_name}" \
                            f"_{int(start_time)}.h5"
            with h5py.File(save_filename, "w") as hf:
                dset = hf.create_dataset("images", data=np.array(images))
                for name, val in (outputs | kwargs).items():
                    dset.attrs[name] = val

            outputs["save_filename"] = save_filename

        return outputs

    def test_measurement(self):
        """ test the beam size measurement w/o saving data """
        old_visualize_state = copy(self.visualize)
        old_save_location = copy(self.save_image_location)
        self.visualize = True
        self.save_image_location = None
        results = self.measure_beamsize(n_shots=1)
        self.visualize = old_visualize_state
        self.save_image_location = old_save_location

        return results

    @property
    def pv_names(self) -> list:
        suffixes = [self.array_data_suffix, self.array_n_cols, self.array_n_rows,
                    self.resolution]
        return [
            f"{self.screen_name}:{ele}" for ele in suffixes
        ]

    @property
    def background_image(self) -> Union[np.ndarray, float]:
        if self.background_file is not None:
            return np.load(self.background_file)
        else:
            return 0.0

    def get_raw_image(self) -> (np.ndarray, float):
        if self.testing:
            img = np.zeros((2000, 2000))
            img[800:-800, 900:-900] = 1
            resolution = 1.0
        else:
            img, nx, ny, resolution = caget_many(self.pv_names)
            img = img.reshape(ny, nx)

        return img, resolution

    def get_processed_image(self):
        img, resolution = self.get_raw_image()

        # subtract background
        img = img - self.background_image
        img = np.where(img >= 0, img, 0)

        # crop image if specified
        if self.roi is not None:
            img = self.roi.crop_image(img)

        return img, resolution

    def measure_background(self, n_measurements: int = 5, file_location: str = None):
        file_location = file_location or ""
        filename = f"{file_location}{self.screen_name}_background.npy".replace(":",
                                                                                "_")
        # insert shutter
        if self.beam_shutter_pv is not None:
            old_shutter_state = caget(self.beam_shutter_pv)
            caput(self.beam_shutter_pv, 1)

        images = []
        for i in range(n_measurements):
            images += [self.get_raw_image()[0]]
            sleep(self.wait_time)

        # restore shutter state
        if self.beam_shutter_pv is not None:
            caput(self.beam_shutter_pv, old_shutter_state)

        # return average
        images = np.stack(images)
        mean = images.mean(axis=0)

        np.save(filename, mean)
        self.background_file = filename

        return mean

    def calculate_beamsize(self, img):
        roi_c = np.array(img.shape) / 2
        roi_radius = np.min((roi_c * 2, np.array(img.shape))) / 2

        fits = self.fit_image(img)
        centroid = fits["centroid"]
        sizes = fits["rms_sizes"]

        # get beam region bounding box
        n_stds = self.bounding_box_half_width
        pts = np.array(
            (
                centroid - n_stds * sizes,
                centroid + n_stds * sizes,
                centroid - n_stds * sizes * np.array((-1, 1)),
                centroid + n_stds * sizes * np.array((-1, 1))
            )
        )

        # visualization
        if self.visualize:
            fig, ax = plt.subplots()
            c = ax.imshow(img, origin="lower")
            ax.plot(*centroid, "+r")
            ax.plot(*roi_c[::-1], ".r")
            fig.colorbar(c)

            rect = patches.Rectangle(pts[0], *sizes * n_stds * 2.0,
                                     facecolor='none',
                                     edgecolor="r")
            ax.add_patch(rect)

            circle = patches.Circle(roi_c[::-1], roi_radius, facecolor="none",
                                    edgecolor="r")
            ax.add_patch(circle)

        distances = np.linalg.norm(pts - roi_c, axis=1)

        # subtract radius to get penalty value
        bb_penalty = np.max(distances) - roi_radius
        log10_total_intensity = fits["log10_total_intensity"]

        result = {
            "Cx": centroid[0],
            "Cy": centroid[1],
            "Sx": sizes[0],
            "Sy": sizes[1],
            "bb_penalty": bb_penalty,
            "total_intensity": fits["total_intensity"],
            "log10_total_intensity": log10_total_intensity
        }

        # set results to none if the beam extends beyond the roi or
        # if the intensity is not greater than a minimum
        if bb_penalty > 0 or log10_total_intensity < self.min_log_intensity:
            for name in ["Cx", "Cy", "Sx", "Sy"]:
                result[name] = np.NaN

        # set bb penalty to None if there is no beam
        if log10_total_intensity < self.min_log_intensity:
            result["bb_penalty"] = np.NaN

        return result

    def fit_image(self, img):
        x_projection = np.sum(img, axis=0)
        y_projection = np.sum(img, axis=1)

        # subtract min value from projections
        x_projection = x_projection - x_projection.min()
        y_projection = y_projection - y_projection.min()


        para_x = fit_gaussian_linear_background(
            x_projection, show_plots=self.visualize,
            n_restarts=self.n_fitting_restarts
        )
        para_y = fit_gaussian_linear_background(
            y_projection, show_plots=self.visualize,
            n_restarts=self.n_fitting_restarts
        )

        return {
            "centroid": np.array((para_x[1], para_y[1])),
            "rms_sizes": np.array((para_x[2], para_y[2])),
            "total_intensity": img.sum(),
            "log10_total_intensity": np.log10(img.sum())
        }

    def yaml(self):
        return yaml.dump(self.dict(), default_flow_style=None, sort_keys=False)

    def dump_yaml(self, fname):
        """dump data to file"""
        output = json.loads(self.json())
        with open(fname, "w") as f:
            yaml.dump(output, f)









