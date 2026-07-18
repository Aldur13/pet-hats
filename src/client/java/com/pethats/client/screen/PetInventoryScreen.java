package com.pethats.client.screen;

import com.pethats.menu.PetInventoryMenu;
import net.minecraft.client.gui.GuiGraphicsExtractor;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;
import net.minecraft.client.gui.screens.inventory.InventoryScreen;
import net.minecraft.network.chat.Component;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.player.Inventory;

/** Shows the pet wearing its Ender Crown, its storage slots, its weapon slot, and the player's inventory. */
public class PetInventoryScreen extends AbstractContainerScreen<PetInventoryMenu> {

	private static final int IMAGE_WIDTH = 176;
	private static final int IMAGE_HEIGHT = 176;
	private static final int PREVIEW_X1 = 9;
	private static final int PREVIEW_Y1 = 9;
	private static final int PREVIEW_X2 = 53;
	private static final int PREVIEW_Y2 = 61;
	private static final int PREVIEW_SCALE = 20;

	private float mouseXState;
	private float mouseYState;

	public PetInventoryScreen(PetInventoryMenu menu, Inventory inventory, Component title) {
		super(menu, inventory, title, IMAGE_WIDTH, IMAGE_HEIGHT);
	}

	@Override
	public void extractRenderState(GuiGraphicsExtractor graphics, int mouseX, int mouseY, float partialTick) {
		this.mouseXState = mouseX;
		this.mouseYState = mouseY;
		super.extractRenderState(graphics, mouseX, mouseY, partialTick);
	}

	@Override
	public void extractBackground(GuiGraphicsExtractor graphics, int mouseX, int mouseY, float partialTick) {
		super.extractBackground(graphics, mouseX, mouseY, partialTick);

		int x = leftPos;
		int y = topPos;
		graphics.fill(x, y, x + imageWidth, y + imageHeight, 0xF0242424);
		graphics.fill(x + PREVIEW_X1 - 1, y + PREVIEW_Y1 - 1, x + PREVIEW_X2 + 1, y + PREVIEW_Y2 + 1, 0xFF000000);
		graphics.fill(x + PREVIEW_X1, y + PREVIEW_Y1, x + PREVIEW_X2, y + PREVIEW_Y2, 0xFF3A3A3A);

		LivingEntity pet = menu.pet();
		if (pet != null) {
			InventoryScreen.extractEntityInInventoryFollowsMouse(
					graphics,
					x + PREVIEW_X1, y + PREVIEW_Y1, x + PREVIEW_X2, y + PREVIEW_Y2,
					PREVIEW_SCALE, 0.25F,
					mouseXState, mouseYState,
					pet);
		}
	}
}
