package com.pethats.item;

import java.util.function.Consumer;
import net.minecraft.ChatFormatting;
import net.minecraft.network.chat.Component;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.TooltipFlag;
import net.minecraft.world.item.component.TooltipDisplay;

/** An item that can be worn by a tamed animal to grant it {@link HatStats}. */
public class HatItem extends Item {
	private final HatStats stats;

	public HatItem(Properties properties, HatStats stats) {
		super(properties);
		this.stats = stats;
	}

	public HatStats stats() {
		return stats;
	}

	@Override
	public void appendHoverText(
			ItemStack stack, TooltipContext context, TooltipDisplay tooltipDisplay, Consumer<Component> tooltipAdder, TooltipFlag flag) {
		super.appendHoverText(stack, context, tooltipDisplay, tooltipAdder, flag);
		tooltipAdder.accept(Component.translatable(getDescriptionId() + ".tooltip").withStyle(ChatFormatting.GRAY));
	}
}
