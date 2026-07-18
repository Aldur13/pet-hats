package com.pethats.client.mixin;

import com.mojang.blaze3d.vertex.PoseStack;
import net.minecraft.client.model.EntityModel;
import net.minecraft.client.model.geom.ModelPart;
import net.minecraft.client.renderer.SubmitNodeCollector;
import net.minecraft.client.renderer.entity.LivingEntityRenderer;
import net.minecraft.client.renderer.entity.state.LivingEntityRenderState;
import net.minecraft.client.renderer.item.ItemStackRenderState;
import net.minecraft.client.renderer.state.level.CameraRenderState;
import net.minecraft.client.renderer.texture.OverlayTexture;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Renders a pet's worn hat on its head. Vanilla already resolves any non-armor item in the entity's HEAD
 * equipment slot into {@code state.headItem} (see {@code LivingEntityRenderer#extractRenderState}); this
 * mixin just draws it, at the "head" model part, for entities whose model exposes one — which covers
 * wolves, cats, parrots, horses and friends generically without a per-species renderer.
 */
@Mixin(LivingEntityRenderer.class)
public abstract class LivingEntityRendererMixin {

	@Shadow
	@SuppressWarnings("rawtypes")
	public abstract EntityModel getModel();

	@Inject(
			method = "submit(Lnet/minecraft/client/renderer/entity/state/LivingEntityRenderState;Lcom/mojang/blaze3d/vertex/PoseStack;Lnet/minecraft/client/renderer/SubmitNodeCollector;Lnet/minecraft/client/renderer/state/level/CameraRenderState;)V",
			at = @At(value = "INVOKE", target = "Lcom/mojang/blaze3d/vertex/PoseStack;popPose()V"))
	@SuppressWarnings("unchecked")
	private void petHats$renderHat(
			LivingEntityRenderState state, PoseStack poseStack, SubmitNodeCollector collector, CameraRenderState camera, CallbackInfo ci) {
		ItemStackRenderState headItem = state.headItem;
		if (headItem.isEmpty()) {
			return;
		}

		EntityModel model = getModel();
		ModelPart root = model.root();
		if (!root.hasChild("head")) {
			return;
		}

		model.setupAnim(state);
		poseStack.pushPose();
		root.getChild("head").translateAndRotate(poseStack);
		headItem.submit(poseStack, collector, state.lightCoords, OverlayTexture.NO_OVERLAY, state.outlineColor);
		poseStack.popPose();
	}
}
