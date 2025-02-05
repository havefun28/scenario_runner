import os, sys
sys.path.append('/home/ruihan/coiltraine/')
import yaml

import torch

from network.models.coil_icra import CoILICRA
from coilutils import AttributeDict

# from attribute_dict import AttributeDict

# # Sample from PyTorch docs: https://pytorch.org/tutorials/beginner/saving_loading_models.html#warmstarting-model-using-parameters-from-a-different-model
# # save
# torch.save(modelA.state_dict(), PATH)

# # load
# device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
# modelB = TheModelBClass(*args, **kwargs)
# modelB.load_state_dict(torch.load(PATH, map_location = device), strict=False)

# # Sample load a pretrained model
# load part of the pre trained model
# save
# torch.save(pre_model.state_dict(), PATH)

# # load
# pretrained_dict = torch.load(PATH)
# model = TheModelClass(*args, **kwargs)
# model_dict = model.state_dict()
# # 1. filter out unnecessary keys
# pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
# # 2. overwrite entries in the existing state dict
# model_dict.update(pretrained_dict) 
# # 3. load the new state dict
# model.load_state_dict(model_dict)


torch.set_default_dtype(torch.float32)
torch.set_default_tensor_type('torch.cuda.FloatTensor')

# read yaml file
yaml_filename = 'coil_configs.yaml'
with open(yaml_filename, 'r') as f:
    # TODO: combine all know configuraitons into one file and load it into a dict
    yaml_file = yaml.load(f, Loader=yaml.FullLoader)
    yaml_cfg = AttributeDict(yaml_file)

# # load checkpoint dict
# checkpoint = torch.load(os.path.join('/home/ruihan/scenario_runner/models/CoIL/'+str(180000)+'.pth'))

# # load model
# model = CoILModel(yaml_cfg.MODEL_TYPE, yaml_cfg.MODEL_CONFIGURATION)
# model.cuda()
# checkpoint_iteration = checkpoint['iteration']
# print("Pretrained CoIL loaded ", checkpoint_iteration)
# model.load_state_dict(checkpoint['state_dict'])
# model.eval()
# torch.save(model.state_dict(), '/home/ruihan/scenario_runner/models/CoIL/CoIL_180000.pth' )

print("load empty CoIlModel")
modelB = CoILICRA(yaml_cfg.MODEL_CONFIGURATION)
for param_tensor in modelB.state_dict():
	print(param_tensor, "\t", modelB.state_dict()[param_tensor].size())

param_tensor = 'branches.branched_modules.0.layers.0.0.weight'
print(param_tensor, "\t", modelB.state_dict()[param_tensor])

print("try to copy pretrained model to B")
modelB.load_state_dict(torch.load('models/CoIL/CoIL_180000.pth'))
print(param_tensor, "\t", modelB.state_dict()[param_tensor])
modelB.eval()

# TODO: The structure is specified in coil_icra. 
# check which module you want to reuse and create your own.
# then load the state_dict with `strict=False`


class FC_coil_cut(nn.Module):
	"""
	copy the full-connectin network from coil, adpted for MLP controller
	"""
	def __init__(self, nx=106, ny=2, nh=53, p=0.2):
		"""
		original coil (512-256-3)
		input: latent_embeddings dim_z = 106
		one hidden layer: 64
		output: dim_u = 3
		p: possibility for dropout
		"""
		super(FC_coil, self).__init__()
		self.layers = nn.Sequential(
			nn.Linear(nx, nh),
			nn.Dropout2d(p=p),
			nn.ReLU(),
			nn.Linear(nh, ny),
			nn.Dropout2d(p=p)
		)
		self.sig = nn.Sigmoid()
		self.tanh = nn.Tanh()

	def forward(self, x):
		x = x.view(x.size(0), -1)
		x = self.layers(x)

		# throttle = self.sig(x[:, 0]).view(x.shape[0],-1)
		# steer = self.tanh(x[:, 1]).view(x.shape[0],-1)
		# brake = self.sig(x[:, 2]).view(x.shape[0],-1)

		# return torch.cat([throttle, steer, brake], dim=1)
		return self.sig(x)