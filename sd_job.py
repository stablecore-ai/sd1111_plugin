import math
import random
from typing import Any, Dict

import numpy as np
import torch
from PIL import Image, ImageFilter, ImageOps

import src_plugins.sd1111_plugin.__conf__
from src_core import plugins
# some of those options should not be changed at all because they would break the model, so I removed them from options.
from src_plugins.sd1111_plugin import images, masking, modelsplit, prompt_parser, sd_hijack, __conf__, SDState
from src_core.lib import devices, imagelib
from src_plugins.sd1111_plugin.options import opts
from src_core.classes.prompt_job import prompt_job

opt_C = 4
opt_f = 8
eta_noise_seed_delta = 0


class sd_job(prompt_job):
    def __init__(self,
                 sampler: int = 'euler-a',
                 seed: int = -1,
                 subseed: int = -1,
                 subseed_force: float = 0,
                 seed_resize_from_h: int = -1,
                 seed_resize_from_w: int = -1,
                 seed_enable_extras: bool = True,
                 batch_size: int = 1,
                 steps: int = 22,
                 cfg: float = 7,
                 w: int = 512,
                 h: int = 512,
                 chg: float = 0.5,
                 tiling: bool = False,
                 extra_generation_params: Dict[Any, Any] = None,
                 overlay_images: Any = None,
                 promptneg: str = None,
                 eta: float = None,
                 ddim_discretize: str = 'uniform',  # [ 'uniform', 'quad' ]
                 s_churn: float = 0.0,
                 s_tmax: float = None,
                 s_tmin: float = 0.0,
                 s_noise: float = 1.0, **kwargs):
        super(sd_job, self).__init__(**kwargs)
        self.promptneg: str = promptneg or ""
        self.seed: int = int(seed)
        self.subseed: int = subseed
        self.subseed_strength: float = subseed_force
        self.seed_resize_from_h: int = seed_resize_from_h
        self.seed_resize_from_w: int = seed_resize_from_w
        self.width: int = int(w)
        self.height: int = int(h)
        self.cfg: float = float(cfg)
        self.sampler_id: int = sampler
        self.batch_size: int = batch_size
        self.steps: int = int(steps)
        self.tiling: bool = bool(tiling)
        self.extra_generation_params: dict = extra_generation_params or {}
        self.overlay_images = overlay_images
        self.eta = eta
        self.chg: float = chg
        self.sampler_noise_scheduler_override = None
        self.ddim_discretize = ddim_discretize
        self.s_churn = s_churn
        self.s_tmin = s_tmin
        self.s_tmax = s_tmax or float('inf')  # not representable as a standard ui option
        self.s_noise = s_noise

        if not seed_enable_extras:
            self.subseed = -1
            self.subseed_strength = 0
            self.seed_resize_from_h = 0
            self.seed_resize_from_w = 0

        self.all_prompts = None
        self.all_seeds = None
        self.all_subseeds = None

    def init(self, model, all_prompts, all_seeds, all_subseeds):
        pass

    def sample(self, conditioning, unconditional_conditioning, seeds, subseeds, subseed_strength):
        raise NotImplementedError()


class sd_txt(sd_job):
    def __init__(self,
                 enable_hr: bool = False,
                 chg: float = 0.75,  # short for "change", also known as denoising strength
                 w1: int = 0,  # first phase width
                 h1: int = 0,  # first phase height
                 **kwargs):
        super().__init__(**kwargs)
        self.enable_hr = enable_hr
        self.chg = chg
        self.w1 = w1  # First phase width
        self.h1 = h1  # First phase height
        self.truncate_x = 0
        self.truncate_y = 0

    def init(self, model, all_prompts, all_seeds, all_subseeds):
        from src_plugins.sd1111_plugin import sd_samplers

        self.sdmodel = model
        self.sampler = sd_samplers.create_sampler(self.sampler_id, model)
        if self.enable_hr:
            # if state.job_count == -1:
            #     state.job_count = self.n_iter * 2
            # else:
            #     state.job_count = state.job_count * 2

            self.extra_generation_params["First pass size"] = f"{self.w1}x{self.h1}"

            if self.w1 == 0 or self.h1 == 0:
                desired_pixel_count = 512 * 512
                actual_pixel_count = self.width * self.height
                scale = math.sqrt(desired_pixel_count / actual_pixel_count)
                self.w1 = math.ceil(scale * self.width / 64) * 64
                self.h1 = math.ceil(scale * self.height / 64) * 64
                firstphase_width_truncated = int(scale * self.width)
                firstphase_height_truncated = int(scale * self.height)
            else:
                width_ratio = self.width / self.w1
                height_ratio = self.height / self.h1

                if width_ratio > height_ratio:
                    firstphase_width_truncated = self.w1
                    firstphase_height_truncated = self.w1 * self.height / self.width
                else:
                    firstphase_width_truncated = self.h1 * self.width / self.height
                    firstphase_height_truncated = self.h1

            self.truncate_x = int(self.w1 - firstphase_width_truncated) // opt_f
            self.truncate_y = int(self.h1 - firstphase_height_truncated) // opt_f

    def create_dummy_mask(self, x, width=None, height=None):
        if self.sampler.conditioning_key in {'hybrid', 'concat'}:
            height = height or self.height
            width = width or self.width

            # The "masked-image" in this case will just be all zeros since the entire image is masked.
            image_conditioning = torch.zeros(x.shape[0], 3, height, width, device=x.device)
            image_conditioning = self.sdmodel.get_first_stage_encoding(self.sdmodel.encode_first_stage(image_conditioning))

            # Add the fake full 1s mask to the first dimension.
            image_conditioning = torch.nn.functional.pad(image_conditioning, (0, 0, 0, 0, 1, 0), value=1.0)
            image_conditioning = image_conditioning.to(x.dtype)

        else:
            # Dummy zero conditioning if we're not using inpainting model.
            # Still takes up a bit of memory, but no encoder call.
            # Pretty sure we can just make this a 1x1 image since its not going to be used besides its batch size.
            image_conditioning = torch.zeros(x.shape[0], 5, 1, 1, dtype=x.dtype, device=x.device)

        return image_conditioning

    def sample(self, conditioning, unconditional_conditioning, seeds, subseeds, subseed_strength):
        if not self.enable_hr:
            x = create_random_tensors([opt_C, self.height // opt_f, self.width // opt_f], seeds=seeds, subseeds=subseeds, subseed_strength=self.subseed_strength, seed_resize_from_h=self.seed_resize_from_h, seed_resize_from_w=self.seed_resize_from_w, p=self)
            samples = self.sampler.sample(self, x, conditioning, unconditional_conditioning, image_conditioning=self.create_dummy_mask(x))
            return samples

        x = create_random_tensors([opt_C, self.h1 // opt_f, self.w1 // opt_f], seeds=seeds, subseeds=subseeds, subseed_strength=self.subseed_strength, seed_resize_from_h=self.seed_resize_from_h, seed_resize_from_w=self.seed_resize_from_w, p=self)
        samples = self.sampler.sample(self, x, conditioning, unconditional_conditioning, image_conditioning=self.create_dummy_mask(x, self.w1, self.h1))

        samples = samples[:, :, self.truncate_y // 2:samples.shape[2] - self.truncate_y // 2, self.truncate_x // 2:samples.shape[3] - self.truncate_x // 2]

        if src_plugins.sd1111_plugin.__conf__.use_scale_latent_for_hires_fix:
            samples = torch.nn.functional.interpolate(samples, size=(self.height // opt_f, self.width // opt_f), mode="bilinear")

        else:
            decoded_samples = decode_first_stage(self.sdmodel, samples)
            lowres_samples = torch.clamp((decoded_samples + 1.0) / 2.0, min=0.0, max=1.0)

            batch_images = []
            for i, x_sample in enumerate(lowres_samples):
                x_sample = 255. * np.moveaxis(x_sample.cpu().numpy(), 0, 2)
                x_sample = x_sample.astype(np.uint8)
                image = Image.fromarray(x_sample)
                image = imagelib.resize_image('lanczos', image, self.width, self.height)
                image = np.array(image).astype(np.float32) / 255.0
                image = np.moveaxis(image, 2, 0)
                batch_images.append(image)

            decoded_samples = torch.from_numpy(np.array(batch_images))
            decoded_samples = decoded_samples.to(devices.device)
            decoded_samples = 2. * decoded_samples - 1.

            samples = self.sdmodel.get_first_stage_encoding(self.sdmodel.encode_first_stage(decoded_samples))

        # SDPlugin.state.nextjob()
        # self.sampler = sd_samplers.create_sampler_with_index(sd_samplers.samplers, self.sampler_index, self.sd_model)

        noise = create_random_tensors(samples.shape[1:], seeds=seeds, subseeds=subseeds, subseed_strength=subseed_strength, seed_resize_from_h=self.seed_resize_from_h, seed_resize_from_w=self.seed_resize_from_w, p=self)

        # GC now before running the next img2img to prevent running out of memory
        x = None
        devices.torch_gc()

        samples = self.sampler.sample_img2img(self, samples, noise, conditioning, unconditional_conditioning, steps=self.steps, image_conditioning=self.create_dummy_mask(samples))

        return samples


class sd_img(sd_job):
    def __init__(self,
                 image: list = None,
                 resize: str = 'lanczos',
                 chg: float = 0.75,
                 mask: Image.Image = None,
                 mask_blur: int = 4,
                 mask_invert: bool = 0,
                 inpaint_fill: int = 0,
                 inpaint_fullres: bool = True,
                 inpaint_fullres_pad: int = 0,
                 **kwargs):
        super().__init__(**kwargs)

        self.init_images = image
        self.init_latent = None
        self.resize_mode = resize
        self.chg = chg
        self.img_mask = mask
        self.mask_blur = mask_blur
        self.mask_inv = mask_invert
        self.inpaint_fill = inpaint_fill
        self.inpaint_fullres = inpaint_fullres
        self.inpaint_fullres_pad = inpaint_fullres_pad

        # State
        self.mask = None # tensor
        self.maskm1 = None # tensor (1-mask)
        self.lmask = None # tensor (latent encoding)
        self.condmask = None
        self.overlay_mask = None # TODO idk what overlay is

    def init(self, model, all_prompts, all_seeds, all_subseeds):
        from src_plugins.sd1111_plugin import sd_samplers

        self.sd_model = model
        self.sampler = sd_samplers.create_sampler(self.sampler_id, model)

        crop_region = None

        if self.img_mask is not None:
            self.img_mask = self.img_mask.convert('L')

            if self.mask_inv:
                self.img_mask = ImageOps.invert(self.img_mask)

            if self.mask_blur > 0:
                self.img_mask = self.img_mask.filter(ImageFilter.GaussianBlur(self.mask_blur))

            if self.inpaint_fullres:
                self.overlay_mask = self.img_mask
                mask = self.img_mask.convert('L')
                crop_region = masking.get_crop_region(np.array(mask), self.inpaint_fullres_pad)
                crop_region = masking.expand_crop_region(crop_region, self.width, self.height, mask.width, mask.height)
                x1, y1, x2, y2 = crop_region

                mask = mask.crop(crop_region)
                self.img_mask = imagelib.resize_image(2, mask, self.width, self.height)
                self.paste_to = (x1, y1, x2 - x1, y2 - y1)
            else:
                self.img_mask = imagelib.resize_image(self.resize_mode, self.img_mask, self.width, self.height)
                np_mask = np.array(self.img_mask)
                np_mask = np.clip((np_mask.astype(np.float32)) * 2, 0, 255).astype(np.uint8)
                self.overlay_mask = Image.fromarray(np_mask)

            self.overlay_images = []

        maskl = self.lmask if self.lmask is not None else self.img_mask

        imgs = []
        for img in self.init_images:
            image = img.convert("RGB")

            if crop_region is None:
                image = imagelib.resize_image(self.resize_mode, image, self.width, self.height)

            if self.img_mask is not None:
                image_masked = Image.new('RGBa', (image.width, image.height))
                image_masked.paste(image.convert("RGBA").convert("RGBa"), mask=ImageOps.invert(self.overlay_mask.convert('L')))

                self.overlay_images.append(image_masked.convert('RGBA'))

            if crop_region is not None:
                image = image.crop(crop_region)
                image = imagelib.resize_image(2, image, self.width, self.height)

            if self.img_mask is not None:
                if self.inpaint_fill != 1:
                    image = masking.fill(image, maskl)

            image = np.array(image).astype(np.float32) / 255.0
            image = np.moveaxis(image, 2, 0)

            imgs.append(image)

        if len(imgs) == 1:
            batch_images = np.expand_dims(imgs[0], axis=0).repeat(self.batch_size, axis=0)
            if self.overlay_images is not None:
                self.overlay_images = self.overlay_images * self.batch_size

        elif len(imgs) <= self.batch_size:
            self.batch_size = len(imgs)
            batch_images = np.array(imgs)
        else:
            raise RuntimeError(f"bad number of images passed: {len(imgs)}; expecting {self.batch_size} or less")

        image = torch.from_numpy(batch_images)
        image = 2. * image - 1.
        image = image.to(devices.device)

        self.init_latent = self.sd_model.get_first_stage_encoding(self.sd_model.encode_first_stage(image))

        if self.img_mask is not None:
            init_mask = maskl
            latmask = init_mask.convert('RGB').resize((self.init_latent.shape[3], self.init_latent.shape[2]))
            latmask = np.moveaxis(np.array(latmask, dtype=np.float32), 2, 0) / 255
            latmask = latmask[0]
            latmask = np.around(latmask)
            latmask = np.tile(latmask[None], (4, 1, 1))

            self.mask = torch.asarray(1.0 - latmask).to(devices.device).type(self.sd_model.dtype)
            self.maskm1 = torch.asarray(latmask).to(devices.device).type(self.sd_model.dtype)

            # this needs to be fixed to be done in sample() using actual seeds for batches
            if self.inpaint_fill == 2:
                self.init_latent = self.init_latent * self.mask + create_random_tensors(self.init_latent.shape[1:], all_seeds[0:self.init_latent.shape[0]]) * self.maskm1
            elif self.inpaint_fill == 3:
                self.init_latent = self.init_latent * self.mask

        if self.sampler.conditioning_key in {'hybrid', 'concat'}:
            if self.img_mask is not None:
                conditioning_mask = np.array(self.img_mask.convert("L"))
                conditioning_mask = conditioning_mask.astype(np.float32) / 255.0
                conditioning_mask = torch.from_numpy(conditioning_mask[None, None])

                # Inpainting model uses a discretized mask as input, so we round to either 1.0 or 0.0
                conditioning_mask = torch.round(conditioning_mask)
            else:
                conditioning_mask = torch.ones(1, 1, *image.shape[-2:])

            # Create another latent image, this time with a masked version of the original input.
            conditioning_mask = conditioning_mask.to(image.device)
            conditioning_image = image * (1.0 - conditioning_mask)
            conditioning_image = self.sd_model.get_first_stage_encoding(self.sd_model.encode_first_stage(conditioning_image))

            # Create the concatenated conditioning tensor to be fed to `c_concat`
            conditioning_mask = torch.nn.functional.interpolate(conditioning_mask, size=self.init_latent.shape[-2:])
            conditioning_mask = conditioning_mask.expand(conditioning_image.shape[0], -1, -1, -1)
            self.condmask = torch.cat([conditioning_mask, conditioning_image], dim=1)
            self.condmask = self.condmask.to(devices.device).type(self.sd_model.dtype)
        else:
            self.condmask = torch.zeros(
                    self.init_latent.shape[0], 5, 1, 1,
                    dtype=self.init_latent.dtype,
                    device=self.init_latent.device
            )

    def sample(self, conditioning, unconditional_conditioning, seeds, subseeds, subseed_strength):
        x = create_random_tensors([opt_C, self.height // opt_f, self.width // opt_f], seeds=seeds, subseeds=subseeds, subseed_strength=self.subseed_strength, seed_resize_from_h=self.seed_resize_from_h, seed_resize_from_w=self.seed_resize_from_w, p=self)
        samples = self.sampler.sample_img2img(self, self.init_latent, x, conditioning, unconditional_conditioning, image_conditioning=self.condmask)

        if self.mask is not None:
            samples = samples * self.maskm1 + self.init_latent * self.mask

        del x
        devices.torch_gc()

        return samples


def process_images(p: sd_job):
    p.prompt = plugins.run(prompt_job(p.prompt))
    p.promptneg = plugins.run(prompt_job(p.promptneg))

    if type(p.prompt) == list:
        assert (len(p.prompt) > 0)
    else:
        assert p.prompt is not None

    devices.torch_gc()

    seed = get_fixed_seed(p.seed)
    subseed = get_fixed_seed(p.subseed)

    sd_hijack.model_hijack.apply_circular(p.tiling)
    sd_hijack.model_hijack.clear_comments()

    # SDPlugin.prompt_styles.apply_styles(p)

    if type(p.prompt) == list:
        p.all_prompts = p.prompt
    else:
        p.all_prompts = p.batch_size * [p.prompt]

    if type(seed) == list:
        p.all_seeds = seed
    else:
        p.all_seeds = [int(seed) + (x if p.subseed_strength == 0 else 0) for x in range(len(p.all_prompts))]

    if type(subseed) == list:
        p.all_subseeds = subseed
    else:
        p.all_subseeds = [int(subseed) + x for x in range(len(p.all_prompts))]

    # if os.path.exists(SDPlugin.embeddings_dir) and not p.do_not_reload_embeddings:
    #     modules.stable_diffusion_auto2222.sd_hijack.model_hijack.embedding_db.load_textual_inversion_embeddings()

    ret = []

    with torch.no_grad(), SDState.sdmodel.ema_scope():
        with devices.autocast():
            p.init(SDState.sdmodel, p.all_prompts, p.all_seeds, p.all_subseeds)

        # if state.skipped:
        #     state.skipped = False
        # if state.interrupted:
        #     break

        prompts = p.all_prompts
        seeds = p.all_seeds
        subseeds = p.all_subseeds

        if len(prompts) == 0:
            return

        with devices.autocast():
            uc = prompt_parser.get_learned_conditioning(SDState.sdmodel, len(prompts) * [p.promptneg], p.steps)
            c = prompt_parser.get_multicond_learned_conditioning(SDState.sdmodel, prompts, p.steps)

        with devices.autocast():
            samples_ddim = p.sample(conditioning=c, unconditional_conditioning=uc, seeds=seeds, subseeds=subseeds, subseed_strength=p.subseed_strength)

        samples_ddim = samples_ddim.to(devices.dtype_vae)
        x_samples_ddim = decode_first_stage(SDState.sdmodel, samples_ddim)
        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)

        del samples_ddim

        if src_plugins.sd1111_plugin.__conf__.lowvram or src_plugins.sd1111_plugin.__conf__.medvram:
            modelsplit.send_everything_to_cpu()

        devices.torch_gc()

        # if opts.filter_nsfw:
        #     import modules.safety as safety
        #     x_samples_ddim = modules.safety.censor_batch(x_samples_ddim)

        for i, x_sample in enumerate(x_samples_ddim):
            x_sample = 255. * np.moveaxis(x_sample.cpu().numpy(), 0, 2)
            x_sample = x_sample.astype(np.uint8)

            image = Image.fromarray(x_sample)

            # if opts.samples_save and not p.do_not_save_samples:
            #     images.save_image(image, p.outpath_samples, "", seeds[i], prompts[i], opts.samples_format, metadata=infotext(n, i), p=p)

            ret.append(image)

        del x_samples_ddim

        devices.torch_gc()

    devices.torch_gc()

    if len(ret) == 1:
        return ret[0]

    return ret


def store_latent(decoded):
    # state.current_latent = decoded

    # if opts.show_progress_every_n_steps > 0 and SDPlugin.state.sampling_step % opts.show_progress_every_n_steps == 0:
    #     if not SDPlugin.parallel_processing_allowed:
    #         SDPlugin.state.current_image = sample_to_image(decoded)
    pass


def slerp(val, low, high):
    # from https://discuss.pytorch.org/t/help-regarding-slerp-function-for-generative-model-sampling/32475/3
    low_norm = low / torch.norm(low, dim=1, keepdim=True)
    high_norm = high / torch.norm(high, dim=1, keepdim=True)
    dot = (low_norm * high_norm).sum(1)

    if dot.mean() > 0.9995:
        return low * val + high * (1 - val)

    omega = torch.acos(dot)
    so = torch.sin(omega)
    res = (torch.sin((1.0 - val) * omega) / so).unsqueeze(1) * low + (torch.sin(val * omega) / so).unsqueeze(1) * high
    return res


def create_random_tensors(shape, seeds, subseeds=None, subseed_strength=0.0, seed_resize_from_h=0, seed_resize_from_w=0, p=None):
    xs = []

    # if we have multiple seeds, this means we are working with batch size>1; this then
    # enables the generation of additional tensors with noise that the sampler will use during its processing.
    # Using those pre-generated tensors instead of simple torch.randn allows a batch with seeds [100, 101] to
    # produce the same images as with two batches [100], [101].
    if p is not None and p.sampler is not None and (len(seeds) > 1 and opts.enable_batch_seeds or eta_noise_seed_delta > 0):
        sampler_noises = [[] for _ in range(p.sampler.number_of_needed_noises(p))]
    else:
        sampler_noises = None

    for i, seed in enumerate(seeds):
        noise_shape = shape if seed_resize_from_h <= 0 or seed_resize_from_w <= 0 else (shape[0], seed_resize_from_h // 8, seed_resize_from_w // 8)

        subnoise = None
        if subseeds is not None:
            subseed = 0 if i >= len(subseeds) else subseeds[i]

            subnoise = devices.randn(subseed, noise_shape)

        # randn results depend on device; gpu and cpu get different results for same seed;
        # the way I see it, it's better to do this on CPU, so that everyone gets same result;
        # but the original script had it like this, so I do not dare change it for now because
        # it will break everyone's seeds.
        noise = devices.randn(seed, noise_shape)

        if subnoise is not None:
            noise = slerp(subseed_strength, noise, subnoise)

        if noise_shape != shape:
            x = devices.randn(seed, shape)
            dx = (shape[2] - noise_shape[2]) // 2
            dy = (shape[1] - noise_shape[1]) // 2
            w = noise_shape[2] if dx >= 0 else noise_shape[2] + 2 * dx
            h = noise_shape[1] if dy >= 0 else noise_shape[1] + 2 * dy
            tx = 0 if dx < 0 else dx
            ty = 0 if dy < 0 else dy
            dx = max(-dx, 0)
            dy = max(-dy, 0)

            x[:, ty:ty + h, tx:tx + w] = noise[:, dy:dy + h, dx:dx + w]
            noise = x

        if sampler_noises is not None:
            cnt = p.sampler.number_of_needed_noises(p)

            if eta_noise_seed_delta > 0:
                torch.manual_seed(seed + eta_noise_seed_delta)

            for j in range(cnt):
                sampler_noises[j].append(devices.randn_without_seed(tuple(noise_shape)))

        xs.append(noise)

    if sampler_noises is not None:
        p.sampler.sampler_noises = [torch.stack(n).to(devices.device) for n in sampler_noises]

    x = torch.stack(xs).to(devices.device)
    return x


def decode_first_stage(model, x):
    with devices.autocast(disable=x.dtype == devices.dtype_vae):
        x = model.decode_first_stage(x)

    return x


def get_fixed_seed(seed):
    if seed is None or seed == '' or seed == -1:
        return int(random.randrange(4294967294))

    return seed


def fix_seed(p):
    p.seed = get_fixed_seed(p.seed)
    p.subseed = get_fixed_seed(p.subseed)
