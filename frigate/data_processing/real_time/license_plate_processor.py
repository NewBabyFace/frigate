"""Handle processing images for face detection and recognition."""

import datetime
import logging
import math
import re
from typing import List, Optional, Tuple

import cv2
import numpy as np
import requests
from pyclipper import ET_CLOSEDPOLYGON, JT_ROUND, PyclipperOffset
from shapely.geometry import Polygon

from frigate.comms.inter_process import InterProcessRequestor
from frigate.config import FrigateConfig
from frigate.const import FRIGATE_LOCALHOST
from frigate.embeddings.functions.onnx import GenericONNXEmbedding, ModelTypeEnum
from frigate.util.image import area

from ..types import DataProcessorMetrics
from .api import RealTimeProcessorApi

logger = logging.getLogger(__name__)

MIN_PLATE_LENGTH = 3


class LicensePlateProcessor(RealTimeProcessorApi):
    def __init__(self, config: FrigateConfig, metrics: DataProcessorMetrics):
        super().__init__(config, metrics)
        self.requestor = InterProcessRequestor()
        self.lpr_config = config.lpr
        self.requires_license_plate_detection = (
            "license_plate" not in self.config.objects.all_objects
        )
        self.detected_license_plates: dict[str, dict[str, any]] = {}

        self.ctc_decoder = CTCDecoder()

        self.batch_size = 6

        # Detection specific parameters
        self.min_size = 3
        self.max_size = 960
        self.box_thresh = 0.8
        self.mask_thresh = 0.8

        self.lpr_detection_model = None
        self.lpr_classification_model = None
        self.lpr_recognition_model = None

        if self.config.lpr.enabled:
            self.detection_model = GenericONNXEmbedding(
                model_name="paddleocr-onnx",
                model_file="detection.onnx",
                download_urls={
                    "detection.onnx": "https://github.com/hawkeye217/paddleocr-onnx/raw/refs/heads/master/models/detection.onnx"
                },
                model_size="large",
                model_type=ModelTypeEnum.lpr_detect,
                requestor=self.requestor,
                device="CPU",
            )

            self.classification_model = GenericONNXEmbedding(
                model_name="paddleocr-onnx",
                model_file="classification.onnx",
                download_urls={
                    "classification.onnx": "https://github.com/hawkeye217/paddleocr-onnx/raw/refs/heads/master/models/classification.onnx"
                },
                model_size="large",
                model_type=ModelTypeEnum.lpr_classify,
                requestor=self.requestor,
                device="CPU",
            )

            self.recognition_model = GenericONNXEmbedding(
                model_name="paddleocr-onnx",
                model_file="recognition.onnx",
                download_urls={
                    "recognition.onnx": "https://github.com/hawkeye217/paddleocr-onnx/raw/refs/heads/master/models/recognition.onnx"
                },
                model_size="large",
                model_type=ModelTypeEnum.lpr_recognize,
                requestor=self.requestor,
                device="CPU",
            )

        if self.lpr_config.enabled:
            # all models need to be loaded to run LPR
            self.detection_model._load_model_and_utils()
            self.classification_model._load_model_and_utils()
            self.recognition_model._load_model_and_utils()

    def _detect(self, image: np.ndarray) -> List[np.ndarray]:
        """
        Detect possible license plates in the input image by first resizing and normalizing it,
        running a detection model, and filtering out low-probability regions.

        Args:
            image (np.ndarray): The input image in which license plates will be detected.

        Returns:
            List[np.ndarray]: A list of bounding box coordinates representing detected license plates.
        """
        h, w = image.shape[:2]

        if sum([h, w]) < 64:
            image = self._zero_pad(image)

        resized_image = self._resize_image(image)
        normalized_image = self._normalize_image(resized_image)

        outputs = self.detection_model([normalized_image])[0]
        outputs = outputs[0, :, :]

        boxes, _ = self._boxes_from_bitmap(outputs, outputs > self.mask_thresh, w, h)
        return self._filter_polygon(boxes, (h, w))

    def _classify(
        self, images: List[np.ndarray]
    ) -> Tuple[List[np.ndarray], List[Tuple[str, float]]]:
        """
        Classify the orientation or category of each detected license plate.

        Args:
            images (List[np.ndarray]): A list of images of detected license plates.

        Returns:
            Tuple[List[np.ndarray], List[Tuple[str, float]]]: A tuple of rotated/normalized plate images
                                                            and classification results with confidence scores.
        """
        num_images = len(images)
        indices = np.argsort([x.shape[1] / x.shape[0] for x in images])

        for i in range(0, num_images, self.batch_size):
            norm_images = []
            for j in range(i, min(num_images, i + self.batch_size)):
                norm_img = self._preprocess_classification_image(images[indices[j]])
                norm_img = norm_img[np.newaxis, :]
                norm_images.append(norm_img)

        outputs = self.classification_model(norm_images)

        return self._process_classification_output(images, outputs)

    def _recognize(
        self, images: List[np.ndarray]
    ) -> Tuple[List[str], List[List[float]]]:
        """
        Recognize the characters on the detected license plates using the recognition model.

        Args:
            images (List[np.ndarray]): A list of images of license plates to recognize.

        Returns:
            Tuple[List[str], List[List[float]]]: A tuple of recognized license plate texts and confidence scores.
        """
        input_shape = [3, 48, 320]
        num_images = len(images)

        # sort images by aspect ratio for processing
        indices = np.argsort(np.array([x.shape[1] / x.shape[0] for x in images]))

        for index in range(0, num_images, self.batch_size):
            input_h, input_w = input_shape[1], input_shape[2]
            max_wh_ratio = input_w / input_h
            norm_images = []

            # calculate the maximum aspect ratio in the current batch
            for i in range(index, min(num_images, index + self.batch_size)):
                h, w = images[indices[i]].shape[0:2]
                max_wh_ratio = max(max_wh_ratio, w * 1.0 / h)

            # preprocess the images based on the max aspect ratio
            for i in range(index, min(num_images, index + self.batch_size)):
                norm_image = self._preprocess_recognition_image(
                    images[indices[i]], max_wh_ratio
                )
                norm_image = norm_image[np.newaxis, :]
                norm_images.append(norm_image)

        outputs = self.recognition_model(norm_images)
        return self.ctc_decoder(outputs)

    def _process_license_plate(
        self, image: np.ndarray
    ) -> Tuple[List[str], List[float], List[int]]:
        """
        Complete pipeline for detecting, classifying, and recognizing license plates in the input image.

        Args:
            image (np.ndarray): The input image in which to detect, classify, and recognize license plates.

        Returns:
            Tuple[List[str], List[float], List[int]]: Detected license plate texts, confidence scores, and areas of the plates.
        """
        if (
            self.detection_model.runner is None
            or self.classification_model.runner is None
            or self.recognition_model.runner is None
        ):
            # we might still be downloading the models
            logger.debug("Model runners not loaded")
            return [], [], []

        plate_points = self._detect(image)
        if len(plate_points) == 0:
            return [], [], []

        plate_points = self._sort_polygon(list(plate_points))
        plate_images = [self._crop_license_plate(image, x) for x in plate_points]
        rotated_images, _ = self._classify(plate_images)

        # keep track of the index of each image for correct area calc later
        sorted_indices = np.argsort([x.shape[1] / x.shape[0] for x in rotated_images])
        reverse_mapping = {
            idx: original_idx for original_idx, idx in enumerate(sorted_indices)
        }

        results, confidences = self._recognize(rotated_images)

        if results:
            license_plates = [""] * len(rotated_images)
            average_confidences = [[0.0]] * len(rotated_images)
            areas = [0] * len(rotated_images)

            # map results back to original image order
            for i, (plate, conf) in enumerate(zip(results, confidences)):
                original_idx = reverse_mapping[i]

                height, width = rotated_images[original_idx].shape[:2]
                area = height * width

                average_confidence = conf

                # set to True to write each cropped image for debugging
                if False:
                    save_image = cv2.cvtColor(
                        rotated_images[original_idx], cv2.COLOR_RGB2BGR
                    )
                    filename = f"/config/plate_{original_idx}_{plate}_{area}.jpg"
                    cv2.imwrite(filename, save_image)

                license_plates[original_idx] = plate
                average_confidences[original_idx] = average_confidence
                areas[original_idx] = area

            # Filter out plates that have a length of less than 3 characters
            # Sort by area, then by plate length, then by confidence all desc
            sorted_data = sorted(
                [
                    (plate, conf, area)
                    for plate, conf, area in zip(
                        license_plates, average_confidences, areas
                    )
                    if len(plate) >= MIN_PLATE_LENGTH
                ],
                key=lambda x: (x[2], len(x[0]), x[1]),
                reverse=True,
            )

            if sorted_data:
                return map(list, zip(*sorted_data))

        return [], [], []

    def _resize_image(self, image: np.ndarray) -> np.ndarray:
        """
        Resize the input image while maintaining the aspect ratio, ensuring dimensions are multiples of 32.

        Args:
            image (np.ndarray): The input image to resize.

        Returns:
            np.ndarray: The resized image.
        """
        h, w = image.shape[:2]
        ratio = min(self.max_size / max(h, w), 1.0)
        resize_h = max(int(round(int(h * ratio) / 32) * 32), 32)
        resize_w = max(int(round(int(w * ratio) / 32) * 32), 32)
        return cv2.resize(image, (resize_w, resize_h))

    def _normalize_image(self, image: np.ndarray) -> np.ndarray:
        """
        Normalize the input image by subtracting the mean and multiplying by the standard deviation.

        Args:
            image (np.ndarray): The input image to normalize.

        Returns:
            np.ndarray: The normalized image, transposed to match the model's expected input format.
        """
        mean = np.array([123.675, 116.28, 103.53]).reshape(1, -1).astype("float64")
        std = 1 / np.array([58.395, 57.12, 57.375]).reshape(1, -1).astype("float64")

        image = image.astype("float32")
        cv2.subtract(image, mean, image)
        cv2.multiply(image, std, image)
        return image.transpose((2, 0, 1))[np.newaxis, ...]

    def _boxes_from_bitmap(
        self, output: np.ndarray, mask: np.ndarray, dest_width: int, dest_height: int
    ) -> Tuple[np.ndarray, List[float]]:
        """
        Process the binary mask to extract bounding boxes and associated confidence scores.

        Args:
            output (np.ndarray): Output confidence map from the model.
            mask (np.ndarray): Binary mask of detected regions.
            dest_width (int): Target width for scaling the box coordinates.
            dest_height (int): Target height for scaling the box coordinates.

        Returns:
            Tuple[np.ndarray, List[float]]: Array of bounding boxes and list of corresponding scores.
        """

        mask = (mask * 255).astype(np.uint8)
        height, width = mask.shape
        outs = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        # handle different return values of findContours between OpenCV versions
        contours = outs[0] if len(outs) == 2 else outs[1]

        boxes = []
        scores = []

        for index in range(len(contours)):
            contour = contours[index]

            # get minimum bounding box (rotated rectangle) around the contour and the smallest side length.
            points, min_side = self._get_min_boxes(contour)

            if min_side < self.min_size:
                continue

            points = np.array(points)

            score = self._box_score(output, contour)
            if self.box_thresh > score:
                continue

            polygon = Polygon(points)
            distance = polygon.area / polygon.length

            # Use pyclipper to shrink the polygon slightly based on the computed distance.
            offset = PyclipperOffset()
            offset.AddPath(points, JT_ROUND, ET_CLOSEDPOLYGON)
            points = np.array(offset.Execute(distance * 1.5)).reshape((-1, 1, 2))

            # get the minimum bounding box around the shrunken polygon.
            box, min_side = self._get_min_boxes(points)

            if min_side < self.min_size + 2:
                continue

            box = np.array(box)

            # normalize and clip box coordinates to fit within the destination image size.
            box[:, 0] = np.clip(np.round(box[:, 0] / width * dest_width), 0, dest_width)
            box[:, 1] = np.clip(
                np.round(box[:, 1] / height * dest_height), 0, dest_height
            )

            boxes.append(box.astype("int32"))
            scores.append(score)

        return np.array(boxes, dtype="int32"), scores

    @staticmethod
    def _get_min_boxes(contour: np.ndarray) -> Tuple[List[Tuple[float, float]], float]:
        """
        Calculate the minimum bounding box (rotated rectangle) for a given contour.

        Args:
            contour (np.ndarray): The contour points of the detected shape.

        Returns:
            Tuple[List[Tuple[float, float]], float]: A list of four points representing the
            corners of the bounding box, and the length of the shortest side.
        """
        bounding_box = cv2.minAreaRect(contour)
        points = sorted(cv2.boxPoints(bounding_box), key=lambda x: x[0])
        index_1, index_4 = (0, 1) if points[1][1] > points[0][1] else (1, 0)
        index_2, index_3 = (2, 3) if points[3][1] > points[2][1] else (3, 2)
        box = [points[index_1], points[index_2], points[index_3], points[index_4]]
        return box, min(bounding_box[1])

    @staticmethod
    def _box_score(bitmap: np.ndarray, contour: np.ndarray) -> float:
        """
        Calculate the average score within the bounding box of a contour.

        Args:
            bitmap (np.ndarray): The output confidence map from the model.
            contour (np.ndarray): The contour of the detected shape.

        Returns:
            float: The average score of the pixels inside the contour region.
        """
        h, w = bitmap.shape[:2]
        contour = contour.reshape(-1, 2)
        x1, y1 = np.clip(contour.min(axis=0), 0, [w - 1, h - 1])
        x2, y2 = np.clip(contour.max(axis=0), 0, [w - 1, h - 1])
        mask = np.zeros((y2 - y1 + 1, x2 - x1 + 1), dtype=np.uint8)
        cv2.fillPoly(mask, [contour - [x1, y1]], 1)
        return cv2.mean(bitmap[y1 : y2 + 1, x1 : x2 + 1], mask)[0]

    @staticmethod
    def _expand_box(points: List[Tuple[float, float]]) -> np.ndarray:
        """
        Expand a polygonal shape slightly by a factor determined by the area-to-perimeter ratio.

        Args:
            points (List[Tuple[float, float]]): Points of the polygon to expand.

        Returns:
            np.ndarray: Expanded polygon points.
        """
        polygon = Polygon(points)
        distance = polygon.area / polygon.length
        offset = PyclipperOffset()
        offset.AddPath(points, JT_ROUND, ET_CLOSEDPOLYGON)
        expanded = np.array(offset.Execute(distance * 1.5)).reshape((-1, 2))
        return expanded

    def _filter_polygon(
        self, points: List[np.ndarray], shape: Tuple[int, int]
    ) -> np.ndarray:
        """
        Filter a set of polygons to include only valid ones that fit within an image shape
        and meet size constraints.

        Args:
            points (List[np.ndarray]): List of polygons to filter.
            shape (Tuple[int, int]): Shape of the image (height, width).

        Returns:
            np.ndarray: List of filtered polygons.
        """
        height, width = shape
        return np.array(
            [
                self._clockwise_order(point)
                for point in points
                if self._is_valid_polygon(point, width, height)
            ]
        )

    @staticmethod
    def _is_valid_polygon(point: np.ndarray, width: int, height: int) -> bool:
        """
        Check if a polygon is valid, meaning it fits within the image bounds
        and has sides of a minimum length.

        Args:
            point (np.ndarray): The polygon to validate.
            width (int): Image width.
            height (int): Image height.

        Returns:
            bool: Whether the polygon is valid or not.
        """
        return (
            point[:, 0].min() >= 0
            and point[:, 0].max() < width
            and point[:, 1].min() >= 0
            and point[:, 1].max() < height
            and np.linalg.norm(point[0] - point[1]) > 3
            and np.linalg.norm(point[0] - point[3]) > 3
        )

    @staticmethod
    def _clockwise_order(point: np.ndarray) -> np.ndarray:
        """
        Arrange the points of a polygon in clockwise order based on their angular positions
        around the polygon's center.

        Args:
            point (np.ndarray): Array of points of the polygon.

        Returns:
            np.ndarray: Points ordered in clockwise direction.
        """
        center = point.mean(axis=0)
        return point[
            np.argsort(np.arctan2(point[:, 1] - center[1], point[:, 0] - center[0]))
        ]

    @staticmethod
    def _sort_polygon(points):
        """
        Sort polygons based on their position in the image. If polygons are close in vertical
        position (within 10 pixels), sort them by horizontal position.

        Args:
            points: List of polygons to sort.

        Returns:
            List: Sorted list of polygons.
        """
        points.sort(key=lambda x: (x[0][1], x[0][0]))
        for i in range(len(points) - 1):
            for j in range(i, -1, -1):
                if abs(points[j + 1][0][1] - points[j][0][1]) < 10 and (
                    points[j + 1][0][0] < points[j][0][0]
                ):
                    temp = points[j]
                    points[j] = points[j + 1]
                    points[j + 1] = temp
                else:
                    break
        return points

    @staticmethod
    def _zero_pad(image: np.ndarray) -> np.ndarray:
        """
        Apply zero-padding to an image, ensuring its dimensions are at least 32x32.
        The padding is added only if needed.

        Args:
            image (np.ndarray): Input image.

        Returns:
            np.ndarray: Zero-padded image.
        """
        h, w, c = image.shape
        pad = np.zeros((max(32, h), max(32, w), c), np.uint8)
        pad[:h, :w, :] = image
        return pad

    @staticmethod
    def _preprocess_classification_image(image: np.ndarray) -> np.ndarray:
        """
        Preprocess a single image for classification by resizing, normalizing, and padding.

        This method resizes the input image to a fixed height of 48 pixels while adjusting
        the width dynamically up to a maximum of 192 pixels. The image is then normalized and
        padded to fit the required input dimensions for classification.

        Args:
            image (np.ndarray): Input image to preprocess.

        Returns:
            np.ndarray: Preprocessed and padded image.
        """
        # fixed height of 48, dynamic width up to 192
        input_shape = (3, 48, 192)
        input_c, input_h, input_w = input_shape

        h, w = image.shape[:2]
        ratio = w / h
        resized_w = min(input_w, math.ceil(input_h * ratio))

        resized_image = cv2.resize(image, (resized_w, input_h))

        # handle single-channel images (grayscale) if needed
        if input_c == 1 and resized_image.ndim == 2:
            resized_image = resized_image[np.newaxis, :, :]
        else:
            resized_image = resized_image.transpose((2, 0, 1))

        # normalize
        resized_image = (resized_image.astype("float32") / 255.0 - 0.5) / 0.5

        padded_image = np.zeros((input_c, input_h, input_w), dtype=np.float32)
        padded_image[:, :, :resized_w] = resized_image

        return padded_image

    def _process_classification_output(
        self, images: List[np.ndarray], outputs: List[np.ndarray]
    ) -> Tuple[List[np.ndarray], List[Tuple[str, float]]]:
        """
        Process the classification model output by matching labels with confidence scores.

        This method processes the outputs from the classification model and rotates images
        with high confidence of being labeled "180". It ensures that results are mapped to
        the original image order.

        Args:
            images (List[np.ndarray]): List of input images.
            outputs (List[np.ndarray]): Corresponding model outputs.

        Returns:
            Tuple[List[np.ndarray], List[Tuple[str, float]]]: A tuple of processed images and
            classification results (label and confidence score).
        """
        labels = ["0", "180"]
        results = [["", 0.0]] * len(images)
        indices = np.argsort(np.array([x.shape[1] / x.shape[0] for x in images]))

        outputs = np.stack(outputs)

        outputs = [
            (labels[idx], outputs[i, idx])
            for i, idx in enumerate(outputs.argmax(axis=1))
        ]

        for i in range(0, len(images), self.batch_size):
            for j in range(len(outputs)):
                label, score = outputs[j]
                results[indices[i + j]] = [label, score]
                if "180" in label and score >= self.lpr_config.threshold:
                    images[indices[i + j]] = cv2.rotate(images[indices[i + j]], 1)

        return images, results

    def _preprocess_recognition_image(
        self, image: np.ndarray, max_wh_ratio: float
    ) -> np.ndarray:
        """
        Preprocess an image for recognition by dynamically adjusting its width.

        This method adjusts the width of the image based on the maximum width-to-height ratio
        while keeping the height fixed at 48 pixels. The image is then normalized and padded
        to fit the required input dimensions for recognition.

        Args:
            image (np.ndarray): Input image to preprocess.
            max_wh_ratio (float): Maximum width-to-height ratio for resizing.

        Returns:
            np.ndarray: Preprocessed and padded image.
        """
        # fixed height of 48, dynamic width based on ratio
        input_shape = [3, 48, 320]
        input_h, input_w = input_shape[1], input_shape[2]

        assert image.shape[2] == input_shape[0], "Unexpected number of image channels."

        # dynamically adjust input width based on max_wh_ratio
        input_w = int(input_h * max_wh_ratio)

        # check for model-specific input width
        model_input_w = self.recognition_model.runner.ort.get_inputs()[0].shape[3]
        if isinstance(model_input_w, int) and model_input_w > 0:
            input_w = model_input_w

        h, w = image.shape[:2]
        aspect_ratio = w / h
        resized_w = min(input_w, math.ceil(input_h * aspect_ratio))

        resized_image = cv2.resize(image, (resized_w, input_h))
        resized_image = resized_image.transpose((2, 0, 1))
        resized_image = (resized_image.astype("float32") / 255.0 - 0.5) / 0.5

        padded_image = np.zeros((input_shape[0], input_h, input_w), dtype=np.float32)
        padded_image[:, :, :resized_w] = resized_image

        return padded_image

    @staticmethod
    def _crop_license_plate(image: np.ndarray, points: np.ndarray) -> np.ndarray:
        """
        Crop the license plate from the image using four corner points.

        This method crops the region containing the license plate by using the perspective
        transformation based on four corner points. If the resulting image is significantly
        taller than wide, the image is rotated to the correct orientation.

        Args:
            image (np.ndarray): Input image containing the license plate.
            points (np.ndarray): Four corner points defining the plate's position.

        Returns:
            np.ndarray: Cropped and potentially rotated license plate image.
        """
        assert len(points) == 4, "shape of points must be 4*2"
        points = points.astype(np.float32)
        crop_width = int(
            max(
                np.linalg.norm(points[0] - points[1]),
                np.linalg.norm(points[2] - points[3]),
            )
        )
        crop_height = int(
            max(
                np.linalg.norm(points[0] - points[3]),
                np.linalg.norm(points[1] - points[2]),
            )
        )
        pts_std = np.float32(
            [[0, 0], [crop_width, 0], [crop_width, crop_height], [0, crop_height]]
        )
        matrix = cv2.getPerspectiveTransform(points, pts_std)
        image = cv2.warpPerspective(
            image,
            matrix,
            (crop_width, crop_height),
            borderMode=cv2.BORDER_REPLICATE,
            flags=cv2.INTER_CUBIC,
        )
        height, width = image.shape[0:2]
        if height * 1.0 / width >= 1.5:
            image = np.rot90(image, k=3)
        return image

    def __update_metrics(self, duration: float) -> None:
        self.metrics.alpr_pps.value = (self.metrics.alpr_pps.value * 9 + duration) / 10

    def _detect_license_plate(self, input: np.ndarray) -> tuple[int, int, int, int]:
        """Return the dimensions of the input image as [x, y, width, height]."""
        # TODO: use a small model here to detect plates
        height, width = input.shape[:2]
        return (0, 0, width, height)

    def process_frame(self, obj_data: dict[str, any], frame: np.ndarray):
        """Look for license plates in image."""
        start = datetime.datetime.now().timestamp()

        id = obj_data["id"]

        # don't run for non car objects
        if obj_data.get("label") != "car":
            logger.debug("Not a processing license plate for non car object.")
            return

        # don't run for stationary car objects
        if obj_data.get("stationary") == True:
            logger.debug("Not a processing license plate for a stationary car object.")
            return

        # don't overwrite sub label for objects that have a sub label
        # that is not a license plate
        if obj_data.get("sub_label") and id not in self.detected_license_plates:
            logger.debug(
                f"Not processing license plate due to existing sub label: {obj_data.get('sub_label')}."
            )
            return

        license_plate: Optional[dict[str, any]] = None

        if self.requires_license_plate_detection:
            logger.debug("Running manual license_plate detection.")
            car_box = obj_data.get("box")

            if not car_box:
                return

            rgb = cv2.cvtColor(frame, cv2.COLOR_YUV2RGB_I420)
            left, top, right, bottom = car_box
            car = rgb[top:bottom, left:right]
            license_plate = self._detect_license_plate(car)

            if not license_plate:
                logger.debug("Detected no license plates for car object.")
                return

            license_plate_frame = car[
                license_plate[1] : license_plate[3], license_plate[0] : license_plate[2]
            ]
            license_plate_frame = cv2.cvtColor(license_plate_frame, cv2.COLOR_RGB2BGR)
        else:
            # don't run for object without attributes
            if not obj_data.get("current_attributes"):
                logger.debug("No attributes to parse.")
                return

            attributes: list[dict[str, any]] = obj_data.get("current_attributes", [])
            for attr in attributes:
                if attr.get("label") != "license_plate":
                    continue

                if license_plate is None or attr.get("score", 0.0) > license_plate.get(
                    "score", 0.0
                ):
                    license_plate = attr

            # no license plates detected in this frame
            if not license_plate:
                return

            license_plate_box = license_plate.get("box")

            # check that license plate is valid
            if (
                not license_plate_box
                or area(license_plate_box) < self.config.lpr.min_area
            ):
                logger.debug(f"Invalid license plate box {license_plate}")
                return

            license_plate_frame = cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)
            license_plate_frame = license_plate_frame[
                license_plate_box[1] : license_plate_box[3],
                license_plate_box[0] : license_plate_box[2],
            ]

        # run detection, returns results sorted by confidence, best first
        license_plates, confidences, areas = self._process_license_plate(
            license_plate_frame
        )

        logger.debug(f"Text boxes: {license_plates}")
        logger.debug(f"Confidences: {confidences}")
        logger.debug(f"Areas: {areas}")

        if license_plates:
            for plate, confidence, text_area in zip(license_plates, confidences, areas):
                avg_confidence = (
                    (sum(confidence) / len(confidence)) if confidence else 0
                )

                logger.debug(
                    f"Detected text: {plate} (average confidence: {avg_confidence:.2f}, area: {text_area} pixels)"
                )
        else:
            # no plates found
            logger.debug("No text detected")
            return

        top_plate, top_char_confidences, top_area = (
            license_plates[0],
            confidences[0],
            areas[0],
        )
        avg_confidence = (
            (sum(top_char_confidences) / len(top_char_confidences))
            if top_char_confidences
            else 0
        )

        # Check if we have a previously detected plate for this ID
        if id in self.detected_license_plates:
            prev_plate = self.detected_license_plates[id]["plate"]
            prev_char_confidences = self.detected_license_plates[id]["char_confidences"]
            prev_area = self.detected_license_plates[id]["area"]
            prev_avg_confidence = (
                (sum(prev_char_confidences) / len(prev_char_confidences))
                if prev_char_confidences
                else 0
            )

            # Define conditions for keeping the previous plate
            shorter_than_previous = len(top_plate) < len(prev_plate)
            lower_avg_confidence = avg_confidence <= prev_avg_confidence
            smaller_area = top_area < prev_area

            # Compare character-by-character confidence where possible
            min_length = min(len(top_plate), len(prev_plate))
            char_confidence_comparison = sum(
                1
                for i in range(min_length)
                if top_char_confidences[i] <= prev_char_confidences[i]
            )
            worse_char_confidences = char_confidence_comparison >= min_length / 2

            if (shorter_than_previous or smaller_area) and (
                lower_avg_confidence and worse_char_confidences
            ):
                logger.debug(
                    f"Keeping previous plate. New plate stats: "
                    f"length={len(top_plate)}, avg_conf={avg_confidence:.2f}, area={top_area} "
                    f"vs Previous: length={len(prev_plate)}, avg_conf={prev_avg_confidence:.2f}, area={prev_area}"
                )
                return True

        # Check against minimum confidence threshold
        if avg_confidence < self.lpr_config.threshold:
            logger.debug(
                f"Average confidence {avg_confidence} is less than threshold ({self.lpr_config.threshold})"
            )
            return

        # Determine subLabel based on known plates, use regex matching
        # Default to the detected plate, use label name if there's a match
        sub_label = next(
            (
                label
                for label, plates in self.lpr_config.known_plates.items()
                if any(re.match(f"^{plate}$", top_plate) for plate in plates)
            ),
            top_plate,
        )

        # Send the result to the API
        resp = requests.post(
            f"{FRIGATE_LOCALHOST}/api/events/{id}/sub_label",
            json={
                "camera": obj_data.get("camera"),
                "subLabel": sub_label,
                "subLabelScore": avg_confidence,
            },
        )

        if resp.status_code == 200:
            self.detected_license_plates[id] = {
                "plate": top_plate,
                "char_confidences": top_char_confidences,
                "area": top_area,
            }

        self.__update_metrics(datetime.datetime.now().timestamp() - start)

    def handle_request(self, topic, request_data) -> dict[str, any] | None:
        return

    def expire_object(self, object_id: str):
        if object_id in self.detected_license_plates:
            self.detected_license_plates.pop(object_id)


class CTCDecoder:
    """
    A decoder for interpreting the output of a CTC (Connectionist Temporal Classification) model.

    This decoder converts the model's output probabilities into readable sequences of characters
    while removing duplicates and handling blank tokens. It also calculates the confidence scores
    for each decoded character sequence.
    """

    def __init__(self):
        """
        Initialize the CTCDecoder with a list of characters and a character map.

        The character set includes digits, letters, special characters, and a "blank" token
        (used by the CTC model for decoding purposes). A character map is created to map
        indices to characters.
        """
        self.characters = [
            "blank",
            "0",
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
            ":",
            ";",
            "<",
            "=",
            ">",
            "?",
            "@",
            "A",
            "B",
            "C",
            "D",
            "E",
            "F",
            "G",
            "H",
            "I",
            "J",
            "K",
            "L",
            "M",
            "N",
            "O",
            "P",
            "Q",
            "R",
            "S",
            "T",
            "U",
            "V",
            "W",
            "X",
            "Y",
            "Z",
            "[",
            "\\",
            "]",
            "^",
            "_",
            "`",
            "a",
            "b",
            "c",
            "d",
            "e",
            "f",
            "g",
            "h",
            "i",
            "j",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "q",
            "r",
            "s",
            "t",
            "u",
            "v",
            "w",
            "x",
            "y",
            "z",
            "{",
            "|",
            "}",
            "~",
            "!",
            '"',
            "#",
            "$",
            "%",
            "&",
            "'",
            "(",
            ")",
            "*",
            "+",
            ",",
            "-",
            ".",
            "/",
            " ",
            " ",
        ]
        self.char_map = {i: char for i, char in enumerate(self.characters)}

    def __call__(
        self, outputs: List[np.ndarray]
    ) -> Tuple[List[str], List[List[float]]]:
        """
        Decode a batch of model outputs into character sequences and their confidence scores.

        The method takes the output probability distributions for each time step and uses
        the best path decoding strategy. It then merges repeating characters and ignores
        blank tokens. Confidence scores for each decoded character are also calculated.

        Args:
            outputs (List[np.ndarray]): A list of model outputs, where each element is
                                        a probability distribution for each time step.

        Returns:
            Tuple[List[str], List[List[float]]]: A tuple of decoded character sequences
                                                and confidence scores for each sequence.
        """
        results = []
        confidences = []
        for output in outputs:
            seq_log_probs = np.log(output + 1e-8)
            best_path = np.argmax(seq_log_probs, axis=1)

            merged_path = []
            merged_probs = []
            for t, char_index in enumerate(best_path):
                if char_index != 0 and (t == 0 or char_index != best_path[t - 1]):
                    merged_path.append(char_index)
                    merged_probs.append(seq_log_probs[t, char_index])

            result = "".join(self.char_map[idx] for idx in merged_path)
            results.append(result)

            confidence = np.exp(merged_probs).tolist()
            confidences.append(confidence)

        return results, confidences
