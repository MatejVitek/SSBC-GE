#!/usr/bin/env python3

# Always import these
import os
import sys
from pathlib import Path
from ast import literal_eval
from matej.collections import DotDict, dzip, lmap
from matej import make_module_callable
from matej.parallel import tqdm_joblib
import argparse
from tkinter import *
import tkinter.filedialog as filedialog
from joblib.parallel import Parallel, delayed
from tqdm import tqdm

# Import whatever else is needed
from data.sets import MOBIUS, SMD, SLD
from evaluation.segmentation import *
import itertools as it
import numpy as np
import operator as op
import pickle
from PIL import Image
from scipy.interpolate import interp1d
import sklearn.metrics as skmetrics


# Constants
TRAIN_DATASETS = 'All', 'MASD+SBVPI', 'MASD+SMD', 'SBVPI', 'SMD'
TEST_DATASETS = {ds.__name__: ds for ds in (MOBIUS, SLD, SMD)}


class Main:
	def __init__(self, *args, **kw):
		# Default values
		root = Path('')
		self.models = Path(args[0] if len(args) > 0 else kw.get('root', root/'Models'))
		self.gt = Path(args[1] if len(args) > 1 else kw.get('gt', root/'GT'))
		self.resize = kw.get('resize', (480, 360))

		# Extra keyword arguments
		self.extra = DotDict(**kw)

	def __str__(self):
		return str(vars(self))

	def __call__(self):
		if not self.models.is_dir():
			raise ValueError(f"{self.models} is not a directory.")
		if not self.gt.is_dir():
			raise ValueError(f"{self.gt} is not a directory.")

		self.threshold = np.linspace(0, 1, self.extra.get('interp', self.extra.get('interp_points', 1000)))

		datasets = {name: ds.from_dir(self.gt/name, mask_dir=None) for name, ds in TEST_DATASETS.items()}
		gts = {}
		for name, dataset in datasets.items():
			dataset.shuffle()
			with tqdm_joblib(tqdm(desc=f"Reading GT from {name}", total=len(dataset))):
				gts[name] = dzip(dataset, Parallel(n_jobs=-1)(
					delayed(self._load_gt)(gt_sample)
					for gt_sample in dataset
				))

		models = list(self.models.iterdir())
		with tqdm(models, desc="Evaluating models") as model_pbar:
			for self._model in model_pbar:
				model_pbar.set_postfix(model=self._model.name)
				model_leave = self._model == models[-1]

				tt_configs = list(it.product(TRAIN_DATASETS, TEST_DATASETS))
				with tqdm(tt_configs, desc="On train/test configuration", leave=model_leave) as data_pbar:
					for self._train, self._test in data_pbar:
						data_pbar.set_postfix(train=self._train, test=self._test)
						data_leave = model_leave and (self._train, self._test) == tt_configs[-1]

						self._predictions = self._model/self._train/self._test/'Predictions'
						self._binarised = self._model/self._train/self._test/'Binarised'
						save_f = self._model/f'Pickles/{self._train}_{self._test}.pkl'
						# Check if pickle already exists
						if not self.extra.get('overwrite', False) and save_f.is_file():
							continue
						save_f.parent.mkdir(parents=True, exist_ok=True)
						if not self._predictions.is_dir():
							raise ValueError(f"{self._predictions} is not a directory.")
						if not self._binarised.is_dir():
							raise ValueError(f"{self._binarised} is not a directory.")

						# Read predictions
						with tqdm_joblib(tqdm(datasets[self._test], desc="Reading predictions", leave=data_leave)) as data:
							pred_bin = dzip(datasets[self._test], Parallel(n_jobs=-1)(
								delayed(self._read_image)(gt_sample)
								for gt_sample in data
							))
						# This will filter out non-existing predictions, so the code will still work, but missing predictions should be addressed (otherwise evaluation is unfair)
						pred_bin_gt = {sample: (*pred_bin[sample], gts[self._test][sample]) for sample in datasets[self._test] if pred_bin[sample] is not None}

						# Evaluate predictions against the ground truths
						with tqdm_joblib(tqdm(list(pred_bin_gt.values()), desc="Computing metrics", leave=data_leave)) as data:
							evals_and_plots = Parallel(n_jobs=-1)(
								delayed(self._evaluate_and_plot)(pred, bin_, gt)
								for pred, bin_, gt in data
							)

						evals = {pb: {metric: np.array([ep[pb][metric].mean for ep in evals_and_plots]) for metric in evals_and_plots[0][pb].keys()} for pb in range(2)}
						plots = lmap(op.itemgetter(2), evals_and_plots)
						mean_plot = Plot.mean_and_std(plots, self.threshold)

						# Save results to pickle file
						with open(save_f, 'wb') as f:
							pickle.dump(list(pred_bin_gt.keys()), f)
							pickle.dump(evals, f)
							pickle.dump(plots, f)
							pickle.dump(mean_plot, f)

	def _open_img(self, f, convert=None):
		img = Image.open(f)
		if self.resize:
			img = img.resize(self.resize)
		if convert:
			img = img.convert(convert)
		return img

	def _load_gt(self, gt_sample):
		return np.array(self._open_img(gt_sample.f, '1'), dtype=np.bool_).flatten()

	def _read_image(self, gt_sample):
		pred_f = self._predictions/gt_sample.f.relative_to(self.gt/self._test)
		bin_f = self._binarised/gt_sample.f.relative_to(self.gt/self._test)
		if not pred_f.is_file():
			for ext in '.jpg', '.jpeg', '.png':
				pred_f = pred_f.with_suffix(ext)
				if pred_f.is_file():
					break
			else:
				print(f"Missing prediction file {pred_f}.", file=sys.stderr)
				return None
		if not bin_f.is_file():
			for ext in '.jpg', '.jpeg', '.png':
				bin_f = bin_f.with_suffix(ext)
				if bin_f.is_file():
					break
			else:
				print(f"Missing binarised file {bin_f}.", file=sys.stderr)
				return None

		pred = np.array(self._open_img(pred_f, 'L')).flatten() / 255
		bin_ = np.array(self._open_img(bin_f, '1'), dtype=np.bool_).flatten()
		return pred, bin_

	def _evaluate_and_plot(self, pred, bin_, gt):
		pred_eval = BinaryIntensitySegmentationEvaluation()
		bin_eval = BinarySegmentationEvaluation()

		# Edge case
		if not np.any(pred):
			pred = pred.copy()
			pred[0] = .5
		if not np.any(bin_):
			bin_ = bin_.copy()
			bin_[0] = 1

		with np.errstate(invalid='ignore', divide='ignore'):  # Ignore division by zero as it's handled below
			# Compute P/R curve of probabilistic prediction
			precisions, recalls, thresholds = skmetrics.precision_recall_curve(gt, pred)
		thresholds = np.append(thresholds, 1.)

		# Hack for edge cases (delete points with the same recall - this also deletes any points with precision=0, recall=0)
		recalls[~np.isfinite(recalls)] = 0  # division by zero in above P/R curve should result in 0
		# Get duplicate indices
		idx_sort = np.argsort(recalls)
		sorted_recalls_array = recalls[idx_sort]
		vals, idx_start, count = np.unique(sorted_recalls_array, return_counts=True, return_index=True)
		duplicates = list(filter(lambda x: x.size > 1, np.split(idx_sort, idx_start[1:])))
		if duplicates:
			# We need to delete everything but the one with maximum precision value
			for i, duplicate in enumerate(duplicates):
				duplicates[i] = sorted(duplicate, key=lambda idx: precisions[idx])[:-1]
			to_delete = np.concatenate(duplicates)
			recalls = np.delete(recalls, to_delete)
			precisions = np.delete(precisions, to_delete)
			thresholds = np.delete(thresholds, to_delete)
		# Find threshold with the best F1-score and update scores at this index
		f1scores = 2 * precisions * recalls / (precisions + recalls)
		idx = f1scores.argmax()
		pred_eval.f1score.update(f1scores[idx])
		pred_eval.precision.update(precisions[idx])
		pred_eval.recall.update(recalls[idx])
		pred_eval.iou.compute_and_update(gt, pred >= thresholds[idx])

		# Compute AUC
		pred_eval.auc.compute_and_update(precisions=precisions, recalls=recalls)

		# Binarised prediction
		for metric in bin_eval:
			metric.compute_and_update(gt, bin_)

		plot = Plot(
			recalls,
			precisions,
			thresholds,
			f1scores,
			(recalls[idx], precisions[idx]),
			(bin_eval.recall.last(), bin_eval.precision.last())
		)

		return pred_eval, bin_eval, plot

	def process_command_line_options(self):
		ap = argparse.ArgumentParser(description="Evaluate segmentation results.")
		ap.add_argument('models', type=Path, nargs='?', default=self.models,
		                help="directory with all model predictions - should contain a separate folder for each model with 'Predictions' and 'Binarised' inside")
		ap.add_argument('gt', type=Path, nargs='?', default=self.gt, help="directory with ground truth masks")
		ap.add_argument('-r', '--resize', type=int, nargs=2, help="width and height to resize the images to")
		ap.parse_known_args(namespace=self)

		ap = argparse.ArgumentParser(description="Extra keyword arguments.")
		ap.add_argument('-e', '--extra', nargs=2, action='append', help="any extra keyword-value argument pairs")
		ap.add_argument('-o', '--overwrite', action='store_true', help="overwrite existing data")
		ap.parse_known_args(namespace=self.extra)

		if self.extra.extra:
			for key, value in self.extra.extra:
				try:
					self.extra[key] = literal_eval(value)
				except ValueError:
					self.extra[key] = value
			del self.extra['extra']

	def gui(self):
		gui = GUI(self)
		gui.mainloop()
		return gui.ok


class Plot:
	def __init__(self, recall, precision, threshold=None, f1=None, f1_point=None, bin_point=None):
		self.recall = recall
		self.precision = precision
		self.threshold = threshold
		self.f1 = (2 * precision * recall / (precision + recall)) if f1 is None else f1
		self.f1_point = f1_point
		self.bin_point = bin_point

		# Edge case
		if len(self.recall) < 2:
			if self.recall[0]:
				self.recall = np.array([0, self.recall[0]])
				self.precision = np.array([0, self.precision[0]])
			else:
				self.recall = np.array([0, 1])
				self.precision = np.array([self.precision[0], 0])
		

	@classmethod
	def mean_and_std(cls, plots, interp=1000):
		try:
			iter(interp)
		except TypeError:
			interp = np.linspace(0, 1, interp)

		# Interpolate precision to linspace recall for mean computation
		precision = np.vstack([
			#interp1d(plot.recall, plot.precision, fill_value='extrapolate')(interp)
			interp1d(plot.recall, plot.precision)(interp)
			#interp1d(plot.recall, plot.precision, fill_value=(1, 0))(interp)
			for plot in plots
		])
		bin_points = np.vstack([plot.bin_point for plot in plots])

		# Compute mean graph and standard deviations
		mean, std = precision.mean(0), precision.std(0)

		# Find max F1 point on mean graph
		f1 = F()
		idx = np.array([f1(precision=p, recall=r) for p, r in zip(mean, interp)]).argmax()

		return (
			Plot(interp, mean, f1_point=(interp[idx], mean[idx]), bin_point=bin_points.mean(0)),  # mean
			Plot(interp, mean - std),  # lower std
			Plot(interp, mean + std)   # upper std
		)


class GUI(Tk):
	def __init__(self, argspace, *args, **kw):
		super().__init__(*args, **kw)
		self.args = argspace
		self.ok = False

		self.frame = Frame(self)
		self.frame.pack(fill=BOTH, expand=YES)

		# In grid(), column default is 0, but row default is first empty row.
		row = 0
		self.models_lbl = Label(self.frame, text="Models:")
		self.models_lbl.grid(column=0, row=row, sticky='w')
		self.models_txt = Entry(self.frame, width=60)
		self.models_txt.insert(END, self.args.models)
		self.models_txt.grid(column=1, columnspan=3, row=row)
		self.models_btn = Button(self.frame, text="Browse", command=self.browse_models)
		self.models_btn.grid(column=4, row=row)

		row += 1
		self.gt_lbl = Label(self.frame, text="GT:")
		self.gt_lbl.grid(column=0, row=row, sticky='w')
		self.gt_txt = Entry(self.frame, width=60)
		self.gt_txt.insert(END, self.args.gt)
		self.gt_txt.grid(column=1, columnspan=3, row=row)
		self.gt_btn = Button(self.frame, text="Browse", command=self.browse_gt)
		self.gt_btn.grid(column=4, row=row)

		row += 1
		self.size_lbl = Label(self.frame, text="Size (WxH):")
		self.size_lbl.grid(column=0, row=row, sticky='w')
		self.width_txt = Entry(self.frame, width=10)
		self.width_txt.insert(END, self.args.resize[0])
		self.width_txt.grid(column=1, row=row)
		self.x_lbl = Label(self.frame, text="x")
		self.x_lbl.grid(column=2, row=row)
		self.height_txt = Entry(self.frame, width=10)
		self.height_txt.insert(END, self.args.resize[1])
		self.height_txt.grid(column=3, row=row)

		row += 1
		self.chk_frame = Frame(self.frame)
		self.chk_frame.grid(row=row, columnspan=3, sticky='w')
		self.overwrite_var = BooleanVar()
		self.overwrite_var.set(False)
		self.overwrite_chk = Checkbutton(self.chk_frame, text="Overwrite", variable = self.overwrite_var)
		self.overwrite_chk.grid(sticky='w')

		row += 1
		self.extra_frame = ExtraFrame(self.frame)
		self.extra_frame.grid(row=row, columnspan=3, sticky='w')

		row += 1
		self.ok_btn = Button(self.frame, text="OK", command=self.confirm)
		self.ok_btn.grid(column=1, row=row)
		self.ok_btn.focus()

	def browse_models(self):
		self._browse_dir(self.models_txt)
		
	def browse_gt(self):
		self._browse_dir(self.gt_txt)

	def _browse_dir(self, target_txt):
		init_dir = target_txt.get()
		while not os.path.isdir(init_dir):
			init_dir = os.path.dirname(init_dir)

		new_entry = filedialog.askdirectory(parent=self, initialdir=init_dir)
		if new_entry:
			_set_entry_text(target_txt, new_entry)

	def _browse_file(self, target_txt, exts=None):
		init_dir = os.path.dirname(target_txt.get())
		while not os.path.isdir(init_dir):
			init_dir = os.path.dirname(init_dir)

		if exts:
			new_entry = filedialog.askopenfilename(parent=self, filetypes=exts, initialdir=init_dir)
		else:
			new_entry = filedialog.askopenfilename(parent=self, initialdir=init_dir)

		if new_entry:
			_set_entry_text(target_txt, new_entry)

	def confirm(self):
		self.args.models = Path(self.models_txt.get())
		self.args.gt = Path(self.gt_txt.get())
		self.args.resize = (int(self.width_txt.get()), int(self.height_txt.get())) if self.width_txt.get() and self.height_txt.get() else None
		self.args.extra.overwrite = self.overwrite_var.get()

		for kw in self.extra_frame.pairs:
			key, value = kw.key_txt.get(), kw.value_txt.get()
			if key:
				try:
					self.args.extra[key] = literal_eval(value)
				except ValueError:
					self.args.extra[key] = value

		self.ok = True
		self.destroy()


class ExtraFrame(Frame):
	def __init__(self, *args, **kw):
		super().__init__(*args, **kw)
		self.pairs = []

		self.key_lbl = Label(self, width=30, text="Key", anchor='w')
		self.value_lbl = Label(self, width=30, text="Value", anchor='w')

		self.add_btn = Button(self, text="+", command=self.add_pair)
		self.add_btn.grid()

	def add_pair(self):
		pair_frame = KWFrame(self, pady=2)
		self.pairs.append(pair_frame)
		pair_frame.grid(row=len(self.pairs), columnspan=3)
		self.update_labels_and_button()

	def update_labels_and_button(self):
		if self.pairs:
			self.key_lbl.grid(column=0, row=0, sticky='w')
			self.value_lbl.grid(column=1, row=0, sticky='w')
		else:
			self.key_lbl.grid_remove()
			self.value_lbl.grid_remove()
		self.add_btn.grid(row=len(self.pairs) + 1)


class KWFrame(Frame):
	def __init__(self, *args, **kw):
		super().__init__(*args, **kw)

		self.key_txt = Entry(self, width=30)
		self.key_txt.grid(column=0, row=0)

		self.value_txt = Entry(self, width=30)
		self.value_txt.grid(column=1, row=0)

		self.remove_btn = Button(self, text="-", command=self.remove)
		self.remove_btn.grid(column=2, row=0)

	def remove(self):
		i = self.master.pairs.index(self)
		del self.master.pairs[i]
		for pair in self.master.pairs[i:]:
			pair.grid(row=pair.grid_info()['row'] - 1)
		self.master.update_labels_and_button()
		self.destroy()


def _set_entry_text(entry, txt):
	entry.delete(0, END)
	entry.insert(END, txt)


if __name__ == '__main__':
	main = Main()

	# If CLI arguments, read them
	if len(sys.argv) > 1:
		main.process_command_line_options()

	# Otherwise get them from a GUI
	else:
		if not main.gui():
			# If GUI was cancelled, exit
			sys.exit(0)

	main()

else:
	# Make module callable (python>=3.5)
	def _main(*args, **kw):
		Main(*args, **kw)()
	make_module_callable(__name__, _main)